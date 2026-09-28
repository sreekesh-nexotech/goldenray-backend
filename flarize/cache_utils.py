"""Version-keyed cache invalidation (standard §7.1) and the public GET response cache.

A *namespace* (``"catalog:components"``, ``"pricing:release"`` …) has an integer version stored under
``cv:<namespace>``. Cache keys embed the versions of **every** namespace the payload depends on, so a write that
calls :func:`bump` orphans every dependent key in O(1) — no key enumeration, no scans. Version keys carry a 7-day
TTL; an expired version reads as 1, which only affects entries older than any response TTL.

:func:`bump` increments immediately *and* again after the surrounding transaction commits. The immediate bump
invalidates what readers cached before the write; the post-commit bump orphans anything a concurrent reader cached
from pre-commit data while the transaction was open. Cache failures never break a write (logged, fail-soft).

Public GET endpoints use :func:`cache_response` / :class:`CachedResponseMixin`: the response body is cached under a
key made from the API version, path, query string and namespace versions; responses carry a content ``ETag`` and
``If-None-Match`` gets ``304``.

Two lifetimes, deliberately separate: the **server-side TTL** (``ttl``, default ``PUBLIC_CACHE_TTL_SECONDS``) may be
long because :func:`bump` invalidates Redis the moment Studio saves; the **HTTP** ``Cache-Control: public,
max-age=…`` reaches browsers and CDNs, which ``bump`` cannot, so it never exceeds ``PUBLIC_CACHE_MAX_AGE_SECONDS``
(PLAN §3.1: 60 s) nor the server TTL. A view may lower it further with ``max_age=``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Sequence
from functools import partial, wraps

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder

logger = logging.getLogger("flarize.cache")

VERSION_KEY_PREFIX = "cv:"
INITIAL_VERSION = 1

Namespaces = Sequence[str] | Callable[..., Sequence[str]]


def version_key(namespace: str) -> str:
    if not namespace or not isinstance(namespace, str):
        raise ValueError("Cache namespace must be a non-empty string.")
    return f"{VERSION_KEY_PREFIX}{namespace}"


def _version_ttl() -> int:
    return int(getattr(settings, "CACHE_VERSION_TTL_SECONDS", 7 * 24 * 60 * 60))


def get_versions(namespaces: Iterable[str]) -> dict[str, int]:
    """Current version of each namespace, fetched with one ``get_many``. Missing keys read as 1."""
    unique = sorted(set(namespaces))
    try:
        stored = cache.get_many([version_key(namespace) for namespace in unique])
    except Exception:  # noqa: BLE001 - cache outage must not break reads
        logger.warning("cache get_many failed; treating versions as initial", exc_info=True)
        stored = {}
    return {namespace: int(stored.get(version_key(namespace), INITIAL_VERSION)) for namespace in unique}


def _bump_now(namespaces: Sequence[str]) -> None:
    ttl = _version_ttl()
    for namespace in namespaces:
        key = version_key(namespace)
        try:
            try:
                cache.incr(key)
            except ValueError:
                if not cache.add(key, INITIAL_VERSION + 1, ttl):
                    cache.incr(key)
            cache.touch(key, ttl)
        except Exception:  # noqa: BLE001 - fail-soft: a cache outage never fails a committed write
            logger.warning("cache version bump failed", extra={"namespace": namespace}, exc_info=True)


def bump(*namespaces: str) -> None:
    """Invalidate every cache entry built from ``namespaces`` (now and again after commit)."""
    if not namespaces:
        raise ValueError("bump() needs at least one namespace.")
    names = tuple(sorted(set(namespaces)))
    for name in names:
        version_key(name)
    _bump_now(names)
    connection = transaction.get_connection()
    if connection.in_atomic_block:
        transaction.on_commit(partial(_bump_now, names), robust=True)


def build_key(prefix: str, *parts, versions: dict[str, int] | None = None) -> str:
    """Stable cache key: ``c:<prefix>:<sha256 of parts + versions>``."""
    material = json.dumps([list(parts), sorted((versions or {}).items())], cls=JSONEncoder, sort_keys=True, separators=(",", ":"))
    return f"c:{prefix}:{hashlib.sha256(material.encode()).hexdigest()}"


def _safe_get(key: str):
    try:
        return cache.get(key)
    except Exception:  # noqa: BLE001
        logger.warning("cache get failed", exc_info=True)
        return None


def _safe_set(key: str, value, ttl: int) -> None:
    try:
        cache.set(key, value, ttl)
    except Exception:  # noqa: BLE001
        logger.warning("cache set failed", exc_info=True)


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    candidates = [candidate.strip() for candidate in header.split(",")]
    return any(candidate in (etag, f"W/{etag}") for candidate in candidates)


def _resolve_namespaces(namespaces: Namespaces, view, request) -> list[str]:
    resolved = namespaces(view, request) if callable(namespaces) else namespaces
    resolved = [namespace for namespace in (resolved or ()) if namespace]
    if not resolved:
        raise ImproperlyConfigured(f"{view.__class__.__name__}: cached responses must declare every namespace the payload depends on.")
    return resolved


def http_max_age(ttl: int, max_age: int | None = None) -> int:
    """``Cache-Control`` max-age: the view's ``max_age`` (if any), never above the public budget or the server TTL."""
    budget = int(getattr(settings, "PUBLIC_CACHE_MAX_AGE_SECONDS", 60))
    return max(0, min(ttl, budget, budget if max_age is None else int(max_age)))


def serve_cached(view, request, namespaces: Namespaces, ttl: int | None, producer: Callable[[], Response], *, max_age: int | None = None) -> Response:
    """Serve ``producer()`` through the version-keyed cache (GET/HEAD only; only 200 responses are stored)."""
    if request.method not in ("GET", "HEAD"):
        return producer()
    ttl = int(ttl or getattr(settings, "PUBLIC_CACHE_TTL_SECONDS", 60))
    versions = get_versions(_resolve_namespaces(namespaces, view, request))
    query = sorted((key, sorted(values)) for key, values in request.query_params.lists())
    key = build_key("resp", getattr(request, "version", None) or "", request.path, query, versions=versions)

    entry = _safe_get(key)
    if entry is None:
        response = producer()
        if response.status_code != status.HTTP_200_OK:
            return response
        # The ETag hashes a canonical (key-sorted) body; the stored payload keeps the view's key order, so a cached
        # response is byte-identical to the uncached one (contracts such as the Strapi delivery depend on key order).
        canonical = json.dumps(response.data, cls=JSONEncoder, sort_keys=True, separators=(",", ":"))
        entry = {"data": json.loads(json.dumps(response.data, cls=JSONEncoder)), "etag": '"' + hashlib.sha256(canonical.encode()).hexdigest()[:40] + '"'}
        _safe_set(key, entry, ttl)
        cache_status = "MISS"
    else:
        cache_status = "HIT"

    if _etag_matches(request.headers.get("If-None-Match"), entry["etag"]):
        response = Response(status=status.HTTP_304_NOT_MODIFIED)
    else:
        response = Response(entry["data"])
    response["ETag"] = entry["etag"]
    response["Cache-Control"] = f"public, max-age={http_max_age(ttl, max_age)}"
    response["X-Cache"] = cache_status
    return response


def cache_response(*, namespaces: Namespaces, ttl: int | None = None, max_age: int | None = None):
    """Decorator for a public view's ``get``/``list``/``retrieve`` method (``ttl`` server-side, ``max_age`` HTTP)."""

    def decorator(method):
        @wraps(method)
        def wrapper(view, request, *args, **kwargs):
            return serve_cached(view, request, namespaces, ttl, lambda: method(view, request, *args, **kwargs), max_age=max_age)

        return wrapper

    return decorator


class CachedResponseMixin:
    """Caches ``list`` and ``retrieve`` of a public viewset. Declare ``cache_namespaces`` (all dependencies)."""

    cache_namespaces: Sequence[str] = ()
    cache_ttl: int | None = None
    cache_max_age: int | None = None

    def get_cache_namespaces(self, request) -> Sequence[str]:
        return self.cache_namespaces

    def list(self, request, *args, **kwargs):
        return serve_cached(
            self, request, lambda view, req: view.get_cache_namespaces(req), self.cache_ttl, lambda: super(CachedResponseMixin, self).list(request, *args, **kwargs), max_age=self.cache_max_age
        )

    def retrieve(self, request, *args, **kwargs):
        return serve_cached(
            self, request, lambda view, req: view.get_cache_namespaces(req), self.cache_ttl, lambda: super(CachedResponseMixin, self).retrieve(request, *args, **kwargs), max_age=self.cache_max_age
        )
