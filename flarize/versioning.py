"""The four versioned API surfaces and the helper that mounts every app's URL lists under them.

Each app's ``urls.py`` exports ``staff_urlpatterns``, ``public_urlpatterns``, ``agent_urlpatterns`` and
``customer_urlpatterns``. ``flarize/urls.py`` mounts them here — and nowhere else — so no DRF view can be reached
outside a versioned surface. Versions are URL-major only (DRF ``URLPathVersioning``); the version group is built
from ``settings.API_VERSIONS`` so an unknown version (``/api/v2/…``) never resolves.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

from django.conf import settings
from django.urls import include, re_path

VERSION_PARAM = "version"


@dataclass(frozen=True)
class Surface:
    name: str
    prefix: str  # path prefix before the version segment, e.g. "api/public/"
    attribute: str  # the list each app's urls.py exports for this surface
    description: str


STAFF = Surface("staff", "api/", "staff_urlpatterns", "Studio / staff API (JWT RS256)")
PUBLIC = Surface("public", "api/public/", "public_urlpatterns", "Website API (AllowAny, cached, throttled)")
AGENT = Surface("agent", "api/agent/", "agent_urlpatterns", "Office agents (service token)")
CUSTOMER = Surface("customer", "api/customer/", "customer_urlpatterns", "Customer signed links (+OTP)")

# Order matters: the more specific prefixes are matched first (a staff path can never start with "public/" because
# the staff version segment comes first, but explicit ordering keeps the resolver obvious).
SURFACES: tuple[Surface, ...] = (PUBLIC, AGENT, CUSTOMER, STAFF)


def allowed_versions() -> tuple[str, ...]:
    return tuple(getattr(settings, "API_VERSIONS", ("v1",)))


def version_group() -> str:
    """Regex group matching exactly the allowed versions: ``(?P<version>v1)`` or ``(?P<version>v1|v2)``."""
    return f"(?P<{VERSION_PARAM}>{'|'.join(allowed_versions())})"


def surface_regex(surface: Surface) -> str:
    return rf"^{surface.prefix}{version_group()}/"


def collect(attribute: str, app_labels: list[str] | tuple[str, ...]) -> list:
    """Concatenate ``<app>.urls.<attribute>`` for every app that defines it."""
    patterns: list = []
    for label in app_labels:
        try:
            module = import_module(f"{label}.urls")
        except ModuleNotFoundError as exc:
            if exc.name != f"{label}.urls":
                raise
            continue
        patterns.extend(getattr(module, attribute, []))
    return patterns


def mount_surfaces(app_labels: list[str] | tuple[str, ...]) -> list:
    """``re_path`` entries for the four versioned surfaces."""
    return [re_path(surface_regex(surface), include((collect(surface.attribute, app_labels), surface.name))) for surface in SURFACES]


def _pattern_string(pattern) -> str:
    """Regex-ish string for a URL pattern (``RoutePattern`` routes are converted to their regex)."""
    inner = pattern.pattern
    regex = getattr(inner, "regex", None)
    text = regex.pattern if regex is not None else str(inner)
    return text[1:] if text.startswith("^") else text


def walk_patterns(patterns=None, prefix: str = ""):
    """Yield ``(full_regex, url_pattern)`` for every leaf pattern of the URLconf (or of ``patterns``)."""
    from django.urls import URLPattern, URLResolver, get_resolver

    if patterns is None:
        patterns = get_resolver().url_patterns
    for pattern in patterns:
        text = _pattern_string(pattern)
        if text.endswith("$") and isinstance(pattern, URLResolver):
            text = text[:-1]
        if isinstance(pattern, URLResolver):
            yield from walk_patterns(pattern.url_patterns, prefix + text)
        elif isinstance(pattern, URLPattern):
            yield prefix + text, pattern


def view_class(url_pattern):
    """The DRF/Django class behind a URL pattern's callback (``None`` for function views)."""
    callback = url_pattern.callback
    return getattr(callback, "cls", None) or getattr(callback, "view_class", None)
