"""Staff writes of the EMI configuration lists (module ``emi``: ``view`` reads, ``edit`` writes — PLAN §3.2).

One implementation for ``emi/banks/``, ``emi/interest-rules/``, ``emi/subsidy-rules/`` and ``emi/system-sizes/``,
parametrised by a :class:`RowSpec`:

* create/update/delete run in one transaction, check ``expected_version``, write one audit row
  (``emi.<entity>_created|updated|deleted``) and bump ``emi:config`` (the public config and every calculation);
* the legacy Studio's cross-field rules are enforced here (400 ``validation_error`` naming the field) and again by
  the database checks; a live-uniqueness clash (bank slug, system-size capacity) is 409 ``<entity>_exists``;
* delete is a soft delete; rows are never hard-deleted.

No outbox event: nothing outside this app reacts to calculator configuration, and the public payloads are
invalidated through the cache namespace.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

from django.db import IntegrityError, transaction
from django.utils.text import slugify

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError
from core.services import check_version, stamp_create
from emi.models import Bank, InterestRateRule, SubsidyRule, SystemSize
from flarize.cache_utils import bump

CACHE_NAMESPACE = "emi:config"


def _band(values: dict, low: str, high: str, message: str) -> dict:
    lower, upper = values.get(low), values.get(high)
    if lower is not None and upper is not None and lower > upper:
        return {high: [message]}
    return {}


def _bank_rules(values: dict) -> dict:
    errors: dict = {}
    if values.get("max_loan") and values.get("min_loan") is not None and values["min_loan"] > values["max_loan"]:
        errors["max_loan"] = ["Maximum loan must be at least the minimum."]
    if values.get("approval_max_days") and values.get("approval_min_days") is not None and values["approval_min_days"] > values["approval_max_days"]:
        errors["approval_max_days"] = ["Must be at least the minimum approval days."]
    return errors


def _interest_rules(values: dict) -> dict:
    errors: dict = {}
    errors.update(_band(values, "min_kw", "max_kw", "Upper bound must be greater than or equal to the lower bound."))
    errors.update(_band(values, "min_system_cost", "max_system_cost", "Upper bound must be greater than or equal to the lower bound."))
    errors.update(_band(values, "min_amount", "max_amount", "Upper bound must be greater than or equal to the lower bound."))
    errors.update(_band(values, "effective_from", "effective_to", "The rule cannot end before it starts."))
    rate, floor = values.get("annual_rate"), values.get("min_annual_rate")
    if rate is not None and floor is not None and rate < floor:
        errors["annual_rate"] = ["The starting rate cannot be below the floor rate."]
    return errors


def _subsidy_rules(values: dict) -> dict:
    errors: dict = {}
    errors.update(_band(values, "kw_from", "kw_to", "Upper bound must be greater than or equal to the lower bound."))
    errors.update(_band(values, "effective_from", "effective_to", "The rule cannot end before it starts."))
    return errors


def _size_rules(values: dict) -> dict:
    return _band(values, "price_min", "price_max", "Slider maximum must be at least the minimum.")


@dataclass(frozen=True)
class RowSpec:
    key: str  # URL segment ("interest-rules")
    entity: str  # audit/error vocabulary ("interest_rule")
    model: type
    fields: tuple[str, ...]
    unique_field: str | None = None
    rules: Callable[[dict], dict] = field(default=lambda values: {})
    prepare: Callable[[dict, object | None], dict] | None = None


def _bank_prepare(values: dict, instance) -> dict:
    """A bank without a slug (created, or edited with a blank one) gets a free one from its name, as the legacy
    Studio did; feature bullets are trimmed and blank ones dropped (legacy ``validate_features``)."""
    if (instance is None and not values.get("slug")) or (instance is not None and "slug" in values and not values["slug"]):
        base = slugify(values.get("name") or getattr(instance, "name", "") or "")[:56] or "bank"
        taken = Bank.objects.exclude(pk=getattr(instance, "pk", None))
        slug, number = base, 2
        while taken.filter(slug=slug).exists():
            slug = f"{base}-{number}"
            number += 1
        values["slug"] = slug
    if "features" in values:
        values["features"] = [item.strip() for item in values["features"] if item and item.strip()]
    return values


BANK_FIELDS = (
    "name",
    "abbr",
    "slug",
    "logo_bg",
    "annual_rate",
    "min_loan",
    "max_loan",
    "upfront_requirement",
    "eligibility",
    "cibil_required",
    "processing_fee_pct",
    "processing_fee_note",
    "approval_min_days",
    "approval_max_days",
    "max_tenure_years",
    "features",
    "best_for",
    "is_recommended",
    "sort_order",
    "is_active",
)
INTEREST_RULE_FIELDS = (
    "label",
    "min_kw",
    "max_kw",
    "min_system_cost",
    "max_system_cost",
    "min_amount",
    "max_amount",
    "annual_rate",
    "min_annual_rate",
    "is_locked",
    "priority",
    "is_active",
    "effective_from",
    "effective_to",
)
SUBSIDY_RULE_FIELDS = ("scheme", "label", "kw_from", "kw_to", "amount", "amount_per_kw", "cap_amount", "priority", "is_active", "effective_from", "effective_to")
SYSTEM_SIZE_FIELDS = ("label", "capacity_kw", "price_per_kw", "price_min", "price_max", "monthly_bill_reference", "sort_order", "is_active")

BANKS = RowSpec("banks", "bank", Bank, BANK_FIELDS, "slug", _bank_rules, _bank_prepare)
INTEREST_RULES = RowSpec("interest-rules", "interest_rule", InterestRateRule, INTEREST_RULE_FIELDS, None, _interest_rules)
SUBSIDY_RULES = RowSpec("subsidy-rules", "subsidy_rule", SubsidyRule, SUBSIDY_RULE_FIELDS, None, _subsidy_rules)
SYSTEM_SIZES = RowSpec("system-sizes", "system_size", SystemSize, SYSTEM_SIZE_FIELDS, "capacity_kw", _size_rules)
SPECS = {spec.key: spec for spec in (BANKS, INTEREST_RULES, SUBSIDY_RULES, SYSTEM_SIZES)}


def queryset(spec: RowSpec):
    return spec.model.objects.all()


def _validate(spec: RowSpec, values: dict) -> None:
    errors = spec.rules(values)
    if errors:
        raise DomainError("validation_error", "The values break a rule of this list.", errors=errors)


def _integrity_error(spec: RowSpec, exc: IntegrityError) -> DomainError:
    if "_uniq" in str(exc) and spec.unique_field:
        return Conflict(f"{spec.entity}_exists", "Another live row already has this value.", errors={spec.unique_field: ["Already exists."]})
    return DomainError("validation_error", "The values break a data rule.", errors={"non_field_errors": ["The values break a data rule."]})


@transaction.atomic
def create_row(spec: RowSpec, *, user, data):
    values = {name: data[name] for name in spec.fields if name in data}
    if spec.prepare is not None:
        values = spec.prepare(values, None)
    row = spec.model(**values)
    _validate(spec, {name: getattr(row, name) for name in spec.fields})
    stamp_create(row, user)
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError as exc:
        raise _integrity_error(spec, exc) from None
    record(f"emi.{spec.entity}_created", obj=row, actor=user, after=snapshot(row, spec.fields))
    bump(CACHE_NAMESPACE)
    return row


@transaction.atomic
def update_row(spec: RowSpec, instance, *, user, data, expected_version=None):
    row = spec.model.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    before = snapshot(row, spec.fields)
    values = {name: data[name] for name in spec.fields if name in data}
    if spec.prepare is not None:
        values = spec.prepare(values, row)
    values = {name: value for name, value in values.items() if value != getattr(row, name)}
    if not values:
        return row
    _validate(spec, {**{name: getattr(row, name) for name in spec.fields}, **values})
    try:
        with transaction.atomic():
            row.versioned_update(user, **values)
    except IntegrityError as exc:
        raise _integrity_error(spec, exc) from None
    changed_before, changed_after = changes(before, snapshot(row, spec.fields))
    record(f"emi.{spec.entity}_updated", obj=row, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return row


@transaction.atomic
def delete_row(spec: RowSpec, instance, *, user, expected_version=None) -> None:
    row = spec.model.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    row.soft_delete(user)
    record(f"emi.{spec.entity}_deleted", obj=row, actor=user, before=snapshot(row, spec.fields))
    bump(CACHE_NAMESPACE)


def services_for(spec: RowSpec) -> dict:
    """The ``services`` map of a staff viewset (``core.views.mixins``)."""
    return {"create": partial(create_row, spec), "update": partial(update_row, spec), "destroy": partial(delete_row, spec)}
