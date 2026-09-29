"""Helpers shared by the inventory services: the feature flag, cache namespaces, locks and error shapes."""

from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError

from core.errors import Conflict, DomainError, NotFound
from core.flags import flag_enabled
from core.services import check_version
from flarize.cache_utils import bump

#: The whole app sits behind this flag (PLAN §1.4, D-6): every endpoint answers 404 while it is off, the dashboard
#: leaves the module out, and received procurement batches are not booked.
FLAG = "INVENTORY_STOCK"

# Version-keyed namespaces (standard §7.1) for readers that cache inventory data.
NS_LOCATIONS = "inventory:locations"
NS_STOCK = "inventory:stock"


def stock_enabled() -> bool:
    return flag_enabled(FLAG)


def receiving_location_code() -> str:
    """``settings.INVENTORY_RECEIVING_LOCATION`` (the code of the location receiving committed batches; '' = off)."""
    return str(getattr(settings, "INVENTORY_RECEIVING_LOCATION", "") or "").strip()


def bump_locations() -> None:
    bump(NS_LOCATIONS)


def bump_stock() -> None:
    bump(NS_STOCK)


def validation_error(errors: dict, message: str = "Invalid input.") -> DomainError:
    return DomainError("validation_error", message, errors=errors)


def lock(model, instance, expected_version=None):
    """Re-read ``instance`` with ``SELECT … FOR UPDATE`` (own row only) and check the client's version."""
    row = model.objects.select_for_update(of=("self",)).filter(pk=instance.pk).first()
    if row is None:
        raise NotFound("not_found", f"{model._meta.verbose_name.capitalize()} not found.")
    check_version(row, expected_version)
    return row


def unique_conflict(exc: IntegrityError, mapping: dict[str, tuple[str, str, str]]) -> Conflict | None:
    """Translate a partial-unique violation into a stable 409 (``mapping``: constraint → (code, field, message))."""
    text = str(exc)
    for constraint, (code, field, message) in mapping.items():
        if constraint in text:
            return Conflict(code, message, errors={field: ["Already in use."]})
    return None
