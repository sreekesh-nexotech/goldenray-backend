"""The ``emi_settings`` singleton (``emi/settings/`` staff; the public config and every calculation read it).

* :func:`current_settings` is what every read serves: the stored row, or an unsaved one carrying the defaults
  (``uid``/``updated_at`` null, ``version`` 1) — reads never write (the legacy ``load()`` created the row on read);
* :func:`update_settings` creates the row on the first edit that changes something (audited
  ``emi.settings_created``, race-safe through the singleton index), then edits it with optimistic locking, validates
  the legacy Studio's rules, writes one audit row and bumps ``emi:config``.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from core.errors import DomainError
from core.services import check_version, stamp_create
from emi.models import EmiSettings
from emi.services.rows import CACHE_NAMESPACE
from flarize.cache_utils import bump

FIELDS = (
    "tenure_min_years",
    "tenure_max_years",
    "tenure_default_years",
    "daily_saving_divisor",
    "price_step",
    "down_payment_min_pct",
    "down_payment_max_pct",
    "down_payment_step_pct",
    "down_payment_quick_adds",
    "rate_max",
    "default_annual_rate",
    "panel_life_years",
    "disclaimer_en",
    "disclaimer_ml",
)


def get_settings() -> EmiSettings | None:
    return EmiSettings.objects.first()


def current_settings() -> EmiSettings:
    """The stored settings, or an unsaved row with the defaults (``updated_at`` null until the first edit)."""
    stored = get_settings()
    if stored is not None:
        return stored
    defaults = EmiSettings()
    defaults.uid = None
    defaults.updated_at = None
    defaults.created_at = None
    return defaults


def validate(values: dict) -> None:
    """The legacy Studio's rules (``EmiCalculatorSettingsSerializer.validate``), on the merged values."""
    errors: dict = {}
    low, high, default = values["tenure_min_years"], values["tenure_max_years"], values["tenure_default_years"]
    if low < 1:
        errors["tenure_min_years"] = ["Must be at least 1."]
    if low > high:
        errors["tenure_max_years"] = ["Maximum tenure must be at least the minimum."]
    elif not low <= default <= high:
        errors["tenure_default_years"] = ["Default tenure must fall inside the allowed range."]
    if not values["daily_saving_divisor"]:
        errors["daily_saving_divisor"] = ["Must be greater than zero."]
    if not values["price_step"] or values["price_step"] <= 0:
        errors["price_step"] = ["Must be greater than zero."]
    dp_low, dp_high = values["down_payment_min_pct"], values["down_payment_max_pct"]
    if not Decimal("0") < dp_low <= Decimal("1"):
        errors["down_payment_min_pct"] = ["Must be greater than 0 and at most 1 (100 %)."]
    if not Decimal("0") < dp_high <= Decimal("1"):
        errors["down_payment_max_pct"] = ["Must be greater than 0 and at most 1 (100 %)."]
    elif dp_low > dp_high:
        errors["down_payment_max_pct"] = ["Maximum must be at least the minimum."]
    if not values["down_payment_step_pct"] or values["down_payment_step_pct"] <= 0:
        errors["down_payment_step_pct"] = ["Must be greater than zero."]
    if any(amount <= 0 for amount in values["down_payment_quick_adds"]):
        errors["down_payment_quick_adds"] = ["Quick-add amounts must be greater than zero."]
    if errors:
        raise DomainError("validation_error", "The settings break a rule of the calculator.", errors=errors)


@transaction.atomic
def ensure_settings(user) -> EmiSettings:
    stored = get_settings()
    if stored is not None:
        return stored
    try:
        with transaction.atomic():
            created = EmiSettings()
            stamp_create(created, user)
            created.save()
    except IntegrityError:
        return get_settings()  # created concurrently (singleton index); that request audited it
    record("emi.settings_created", obj=created, actor=user, after=snapshot(created, FIELDS))
    bump(CACHE_NAMESPACE)
    return created


@transaction.atomic
def update_settings(*, user, data: dict, expected_version=None) -> EmiSettings:
    """Edit the settings (``PATCH emi/settings/``); the defaults' version is 1, so a client's first edit carries 1."""
    values = {name: data[name] for name in FIELDS if name in data}
    if "down_payment_quick_adds" in values:
        values["down_payment_quick_adds"] = sorted(set(values["down_payment_quick_adds"]))
    current = current_settings()
    check_version(current, expected_version)
    if not {name: value for name, value in values.items() if value != getattr(current, name)}:
        return current
    # The first edit creates the row (version 1) and edits it (version 2): a client still holding the defaults'
    # version 1 then gets stale_version instead of overwriting a concurrent first edit.
    row = EmiSettings.objects.select_for_update().get(pk=ensure_settings(user).pk)
    check_version(row, expected_version)
    changed = {name: value for name, value in values.items() if value != getattr(row, name)}
    if not changed:
        return row
    validate({**{name: getattr(row, name) for name in FIELDS}, **changed})
    before = snapshot(row, FIELDS)
    try:
        with transaction.atomic():
            row.versioned_update(user, **changed)
    except IntegrityError:
        raise DomainError("validation_error", "The settings break a data rule.", errors={"non_field_errors": ["The settings break a data rule."]}) from None
    changed_before, changed_after = changes(before, snapshot(row, FIELDS))
    record("emi.settings_updated", obj=row, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return row
