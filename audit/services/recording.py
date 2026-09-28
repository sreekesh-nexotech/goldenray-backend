"""Writing audit rows: :func:`record` and the helpers services use to describe a change.

Rules (PLAN §1.4, standard §3.2):

* :func:`record` writes **synchronously inside the caller's transaction** — if the business write rolls back, so
  does its audit row, and a committed write always has one.
* Values under sensitive keys (``password``, ``token``, ``secret``, ``otp``, ``account_number``, ``api_key``,
  ``authorization`` — whole words of the key, any case/style, plurals included) are replaced by ``"***"`` at every
  depth. Raw request bodies are never recorded; services pass explicit before/after snapshots.
* The actor defaults to the one attached to the request's audit context (``audit.context``) when the caller names
  neither ``actor`` nor ``actor_kind``; outside a request the row is attributed to ``SYSTEM``.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

from audit import context
from audit.models import AuditLog

MASK = "***"
SENSITIVE_TERMS: tuple[str, ...] = ("password", "token", "secret", "otp", "account_number", "api_key", "authorization")
ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
OBJECT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")
_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _key_words(key) -> str:
    """``"newPassword"`` / ``"new-password"`` / ``"NEW_PASSWORD"`` → ``"_new_password_"``."""
    words = [word for word in _SPLIT_RE.split(_CAMEL_RE.sub(r"\1_\2", str(key)).lower()) if word]
    return "_" + "_".join(words) + "_"


def is_sensitive_key(key) -> bool:
    joined = _key_words(key)
    return any(f"_{term}_" in joined or f"_{term}s_" in joined for term in SENSITIVE_TERMS)


def mask_sensitive(value):
    """Copy of ``value`` with every sensitive key's value replaced by ``MASK`` (recursively, lists included)."""
    if isinstance(value, Mapping):
        return {str(key): (MASK if is_sensitive_key(key) else mask_sensitive(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_sensitive(item) for item in value]
    return value


def _json_safe(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def _prepare(value, name: str):
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"audit {name} must be a dict (a snapshot of the changed fields), got {type(value).__name__}.")
    return mask_sensitive(_json_safe(dict(value)))


def object_type_of(obj) -> str:
    return f"{obj._meta.app_label}.{obj._meta.model_name}"


def _as_uuid(value) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _resolve_actor(actor, actor_kind: str | None) -> tuple[object | None, str]:
    from core.models import actor_or_none

    if actor is None and actor_kind is None:
        ctx = context.current()
        principal, kind = ctx.actor, ctx.actor_kind
    else:
        principal, kind = actor, actor_kind
    if kind is None:
        if actor_or_none(principal) is not None:
            kind = AuditLog.ActorKind.USER
        elif getattr(principal, "kind", None) in AuditLog.ActorKind.values:
            kind = principal.kind
        else:
            kind = AuditLog.ActorKind.SYSTEM
    if kind not in AuditLog.ActorKind.values:
        raise ValueError(f"Unknown audit actor kind {kind!r}.")
    return actor_or_none(principal), kind


def record(
    action: str,
    *,
    obj=None,
    object_type: str | None = None,
    object_uid=None,
    before: Mapping | None = None,
    after: Mapping | None = None,
    note: str = "",
    actor=None,
    actor_kind: str | None = None,
) -> AuditLog:
    """Append one audit row in the caller's transaction and return it.

    ``action`` is ``<context>.<verb>`` (``accounts.user_deactivated``). ``obj`` (a model instance with ``uid``)
    fills ``object_type``/``object_uid``; pass them explicitly for non-model subjects. ``before``/``after`` are
    dict snapshots (use :func:`snapshot` / :func:`changes`); sensitive keys are masked.
    """
    if not action or len(action) > 64 or not ACTION_RE.match(action):
        raise ValueError(f"Invalid audit action {action!r}; use '<context>.<verb>' (max 64 characters).")
    if obj is not None:
        object_type = object_type or object_type_of(obj)
        object_uid = object_uid if object_uid is not None else getattr(obj, "uid", None)
    object_type = object_type or ""
    if object_type and (len(object_type) > 64 or not OBJECT_TYPE_RE.match(object_type)):
        raise ValueError(f"Invalid audit object type {object_type!r}.")
    user, kind = _resolve_actor(actor, actor_kind)
    ctx = context.current()
    entry = AuditLog(
        at=timezone.now(),
        actor=user,
        actor_kind=kind,
        request_id=_as_uuid(ctx.request_id),
        ip=ctx.ip or None,
        action=action,
        object_type=object_type,
        object_uid=_as_uuid(object_uid),
        before=_prepare(before, "before"),
        after=_prepare(after, "after"),
        note=note or "",
    )
    entry.save()
    return entry


def _snapshot_value(instance, field):
    if isinstance(field, models.ForeignKey):
        related = getattr(instance, field.name)
        if related is None:
            return None
        return str(related.uid) if hasattr(related, "uid") else str(related)
    return field.value_from_object(instance)


def snapshot(instance, fields: Iterable[str]) -> dict:
    """``{field: value}`` for ``fields`` of ``instance``; foreign keys are rendered as the related row's ``uid``."""
    result = {}
    for name in fields:
        field = instance._meta.get_field(name)
        result[name] = _snapshot_value(instance, field)
    return _json_safe(result)


def changes(before: Mapping, after: Mapping) -> tuple[dict, dict]:
    """Only the keys whose values differ, as ``(before, after)``."""
    keys = [key for key in dict.fromkeys([*before, *after]) if before.get(key) != after.get(key)]
    return {key: before.get(key) for key in keys}, {key: after.get(key) for key in keys}
