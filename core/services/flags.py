"""Feature flag writes. Reads go through ``core.flags.flag_enabled`` (cached)."""

from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, transaction

from core.errors import DomainError, NotFound
from core.flags import FLAGS_CACHE_NAMESPACE
from core.models import FeatureFlag
from core.outbox import emit
from core.services.stamping import stamp_create
from core.services.versioning import check_version, save_versioned
from flarize.cache_utils import bump


def known_flags() -> list[str]:
    return sorted(getattr(settings, "FEATURE_FLAG_DEFAULTS", {}))


def list_flags() -> list[dict]:
    """Every known flag with its effective state; ``row`` is the override row or ``None`` (default in force)."""
    defaults = getattr(settings, "FEATURE_FLAG_DEFAULTS", {})
    rows = {row.key: row for row in FeatureFlag.objects.filter(key__in=list(defaults))}
    result = []
    for key in known_flags():
        row = rows.get(key)
        result.append({"key": key, "enabled": row.enabled if row else bool(defaults[key]), "default": bool(defaults[key]), "row": row})
    return result


@transaction.atomic
def set_flag(key: str, *, enabled: bool, user, note: str | None = None, expected_version=None) -> FeatureFlag:
    """Turn a known flag on/off. Creates the override row on first change; versioned afterwards."""
    defaults = getattr(settings, "FEATURE_FLAG_DEFAULTS", {})
    if key not in defaults:
        raise NotFound("unknown_flag", f"Unknown feature flag {key!r}.")
    if not isinstance(enabled, bool):
        raise DomainError("validation_error", "enabled must be a boolean.", errors={"enabled": ["Must be a boolean."]})

    row = FeatureFlag.objects.select_for_update().filter(key=key).first()
    created = False
    if row is None:
        # No override row yet: the default is in force and there is no version to compare against.
        try:
            with transaction.atomic():
                row = FeatureFlag(key=key, enabled=enabled, note=note or "")
                stamp_create(row, user)
                row.save()
            created = True
        except IntegrityError:
            # A concurrent request created the row first; apply this change on top of it.
            row = FeatureFlag.objects.select_for_update().get(key=key)

    if created:
        before = bool(defaults[key])
    else:
        before = row.enabled
        check_version(row, expected_version)
        fields = []
        if row.enabled != enabled:
            row.enabled = enabled
            fields.append("enabled")
        if note is not None and note != row.note:
            row.note = note
            fields.append("note")
        if fields:
            save_versioned(row, user=user, fields=fields)
    bump(FLAGS_CACHE_NAMESPACE)
    if before != enabled:
        emit("core.flag_changed", {"key": key, "enabled": enabled, "previous": before}, aggregate_type="core.feature_flag", aggregate_uid=row.uid)
    from audit.services import record

    record("core.flag_set", obj=row, actor=user, before={"enabled": before}, after={"enabled": enabled, "note": row.note}, note=note or "")
    return row
