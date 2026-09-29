"""Helpers shared by the bom services: cache namespace, events, document validation, percentage ↔ fraction."""

from __future__ import annotations

from decimal import Decimal

from bom import schemas
from core.errors import DomainError
from core.outbox import emit
from flarize.cache_utils import bump

CACHE_NAMESPACE = "bom"
CHANGED_EVENT = "bom.configuration_changed"


def validation_error(errors: dict, message: str = "Invalid input.") -> DomainError:
    return DomainError("validation_error", message, errors=errors)


def check_document(value, schema_name: str, field: str) -> None:
    """400 ``validation_error`` on ``field`` unless ``value`` satisfies the named ``bom.schemas`` schema."""
    found = schemas.problems(value, schema_name)
    if found:
        raise validation_error({field: found}, f"{field} is not a valid document.")


def changed(obj, action: str) -> None:
    """Every bom write: bump the ``bom`` cache namespace and emit ``bom.configuration_changed`` (packs and the
    website quote read this configuration)."""
    bump(CACHE_NAMESPACE)
    emit(CHANGED_EVENT, {"object_type": f"bom.{type(obj).__name__.lower()}", "object_uid": str(obj.uid), "action": action}, aggregate_type=f"bom.{type(obj).__name__.lower()}", aggregate_uid=obj.uid)


def percent_of(fraction: Decimal | None):
    """A stored GST fraction as the legacy percentage number (``Decimal("0.18")`` → ``18``; ``0.125`` → ``12.5``)."""
    if fraction is None:
        return None
    value = Decimal(fraction) * 100
    return int(value) if value == value.to_integral_value() else float(value.normalize())


def fraction_of(percent) -> Decimal | None:
    if percent is None or percent == "":
        return None
    return (Decimal(str(percent)) / 100).quantize(Decimal("0.0001"))
