"""Optimistic locking helpers used by every service that writes a versioned row."""

from __future__ import annotations

from collections.abc import Iterable

from core.errors import DomainError, StaleVersion
from core.models.base import BaseModel


def parse_expected_version(value) -> int | None:
    """Coerce a client-supplied ``expected_version`` (body or query string) to ``int``; ``None`` if absent."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise DomainError("validation_error", "expected_version must be a positive integer.", errors={"expected_version": ["Must be a positive integer."]})
    try:
        version = int(value)
    except (TypeError, ValueError):
        raise DomainError("validation_error", "expected_version must be a positive integer.", errors={"expected_version": ["Must be a positive integer."]}) from None
    if version < 1:
        raise DomainError("validation_error", "expected_version must be a positive integer.", errors={"expected_version": ["Must be a positive integer."]})
    return version


def check_version(instance: BaseModel, expected_version) -> None:
    """Raise ``StaleVersion`` (409 ``stale_version``) when the client edited an older version of ``instance``.

    ``expected_version=None`` means the client did not send one; callers that *require* it validate that in the
    serializer. The authoritative guard is the compare-and-swap in :func:`save_versioned`.
    """
    expected = parse_expected_version(expected_version)
    if expected is not None and expected != instance.version:
        raise StaleVersion(errors={"expected_version": [f"Current version is {instance.version}."]})


def save_versioned(instance: BaseModel, *, user, fields: Iterable[str], expected_version=None) -> BaseModel:
    """Persist ``fields`` of an already-mutated ``instance`` with a compare-and-swap on ``version``.

    Stamps ``updated_by``/``updated_at`` and increments ``version``. Raises ``StaleVersion`` if the client's
    ``expected_version`` is old or a concurrent writer committed first.
    """
    check_version(instance, expected_version)
    names = [name for name in dict.fromkeys(fields) if name not in {"version", "updated_at", "updated_by"}]
    instance.versioned_update(user, **{name: getattr(instance, name) for name in names})
    return instance
