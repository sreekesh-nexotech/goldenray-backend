"""Redirects (staff ``seo/redirects/`` CRUD, public ``seo/redirects/`` for the Next.js build).

* ``from_path`` is site-relative (``/old-page``, Next.js patterns such as ``/blog/:slug`` allowed), without query or
  fragment, unique among live rows (409 ``redirect_exists``);
* ``to_path`` is site-relative or an absolute ``http(s)`` URL; a redirect to itself or one that closes a loop through
  other redirects is refused (400 ``redirect_loop``);
* ``status_code`` is 301/302/307/308; every write is versioned, audited and bumps ``seo:redirects`` (the website picks
  redirects up at its next build — ``next.config`` ``redirects()`` is build-time, so no revalidation is sent).
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from seo.models import Redirect

NAMESPACE = "seo:redirects"
FIELDS = ("from_path", "to_path", "status_code", "note")
_SITE_PATH_RE = re.compile(r"^/(?!/)[^\s?#]*$")
_TARGET_PATH_RE = re.compile(r"^/(?!/)\S*$")
MAX_CHAIN = 20


def _invalid(field: str, message: str, code: str = "validation_error") -> DomainError:
    return DomainError(code, message, errors={field: [message]})


def _validate(values: dict) -> dict:
    if "from_path" in values:
        values["from_path"] = (values["from_path"] or "").strip()
        if len(values["from_path"]) > 500 or not _SITE_PATH_RE.match(values["from_path"]):
            raise _invalid("from_path", "A site-relative path such as '/old-page' (no query string or fragment).")
    if "to_path" in values:
        target = (values["to_path"] or "").strip()
        values["to_path"] = target
        if target.startswith(("http://", "https://")):
            try:
                URLValidator(schemes=["http", "https"])(target)
            except ValidationError:
                raise _invalid("to_path", "Not a valid URL.") from None
        elif not _TARGET_PATH_RE.match(target) or len(target) > 1000:
            raise _invalid("to_path", "A site-relative path such as '/new-page' or an absolute https URL.")
    if "status_code" in values and values["status_code"] not in Redirect.StatusCode.values:
        raise _invalid("status_code", "Use 301, 302, 307 or 308.")
    return values


def _check_loop(from_path: str, to_path: str, *, exclude_pk=None) -> None:
    if from_path == to_path:
        raise _invalid("to_path", "A redirect cannot point at itself.", "redirect_loop")
    targets = dict(Redirect.objects.exclude(pk=exclude_pk).values_list("from_path", "to_path")) if exclude_pk else dict(Redirect.objects.values_list("from_path", "to_path"))
    current, seen = to_path, {from_path}
    for _ in range(MAX_CHAIN):
        if current in seen:
            raise _invalid("to_path", f"This would create a redirect loop through {current}.", "redirect_loop")
        seen.add(current)
        current = targets.get(current)
        if current is None:
            return


def _exists() -> Conflict:
    return Conflict("redirect_exists", "A redirect from this path already exists.", errors={"from_path": ["Already redirected."]})


def redirects_queryset():
    return Redirect.objects.order_by("from_path", "id")


@transaction.atomic
def create_redirect(*, user, data) -> Redirect:
    values = _validate({name: data[name] for name in FIELDS if name in data})
    _check_loop(values["from_path"], values["to_path"])
    redirect = Redirect(**values)
    stamp_create(redirect, user)
    try:
        with transaction.atomic():
            redirect.save()
    except IntegrityError:
        raise _exists() from None
    record("seo.redirect_created", obj=redirect, actor=user, after=snapshot(redirect, FIELDS))
    bump(NAMESPACE)
    return redirect


@transaction.atomic
def update_redirect(instance: Redirect, *, user, data, expected_version=None) -> Redirect:
    redirect = Redirect.objects.select_for_update().get(pk=instance.pk)
    check_version(redirect, expected_version)
    values = _validate({name: data[name] for name in FIELDS if name in data})
    values = {name: value for name, value in values.items() if value != getattr(redirect, name)}
    if not values:
        return redirect
    _check_loop(values.get("from_path", redirect.from_path), values.get("to_path", redirect.to_path), exclude_pk=redirect.pk)
    before = snapshot(redirect, FIELDS)
    try:
        with transaction.atomic():
            redirect.versioned_update(user, **values)
    except IntegrityError:
        raise _exists() from None
    changed_before, changed_after = changes(before, snapshot(redirect, FIELDS))
    record("seo.redirect_updated", obj=redirect, actor=user, before=changed_before, after=changed_after)
    bump(NAMESPACE)
    return redirect


@transaction.atomic
def delete_redirect(instance: Redirect, *, user, expected_version=None) -> None:
    redirect = Redirect.objects.select_for_update().get(pk=instance.pk)
    check_version(redirect, expected_version)
    redirect.soft_delete(user)
    record("seo.redirect_deleted", obj=redirect, actor=user, before=snapshot(redirect, FIELDS))
    bump(NAMESPACE)
