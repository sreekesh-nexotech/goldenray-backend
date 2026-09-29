"""Staff writes of the calculators' sizing tables (module ``reference_data``: view / create / edit / archive).

``calculators/capacity-sizes/`` (legacy ``solar_installations``) and ``calculators/bill-range-sizes/`` (legacy
``solar_installation_new``). Create/update/delete run in one transaction, check ``expected_version``, write one audit
row (``calculators.<entity>_created|updated|deleted``) and bump ``calculators:sizes`` (every calculation reads a
cached snapshot of these tables). A second live row for the same size, or for the same bill range and property
type, is 409 ``<entity>_exists`` — the legacy lookups (``.get(...)``) crashed on such duplicates. Delete is a soft
delete. No outbox event: nothing outside the calculators reacts to these rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from calculators.models import BillRangeSize, CapacitySize
from core.errors import Conflict, DomainError
from core.services import check_version, stamp_create
from flarize.cache_utils import bump

CACHE_NAMESPACE = "calculators:sizes"


@dataclass(frozen=True)
class SizingSpec:
    key: str
    entity: str
    model: type
    fields: tuple[str, ...]
    unique_fields: tuple[str, ...]


CAPACITY_SIZES = SizingSpec(
    "capacity-sizes",
    "capacity_size",
    CapacitySize,
    ("power_capacity_kw", "installation_days", "total_cost", "total_subsidy", "area_required_sqft", "is_active"),
    ("power_capacity_kw",),
)
BILL_RANGE_SIZES = SizingSpec(
    "bill-range-sizes",
    "bill_range_size",
    BillRangeSize,
    (
        "bill_range",
        "property_type",
        "power_capacity_kw",
        "installation_days_range",
        "total_cost",
        "total_subsidy",
        "area_required_sqft",
        "loan_available",
        "per_kw_rate",
        "final_cost",
        "interest_rate",
        "inverter_price",
        "is_active",
    ),
    ("bill_range", "property_type"),
)
SPECS = {spec.key: spec for spec in (CAPACITY_SIZES, BILL_RANGE_SIZES)}


def queryset(spec: SizingSpec):
    return spec.model.objects.all()


def _integrity_error(spec: SizingSpec, exc: IntegrityError) -> DomainError:
    if "_uniq" in str(exc):
        return Conflict(f"{spec.entity}_exists", "Another live row already prices this size.", errors={name: ["Already exists."] for name in spec.unique_fields})
    return DomainError("validation_error", "The values break a data rule.", errors={"non_field_errors": ["The values break a data rule."]})


@transaction.atomic
def create_row(spec: SizingSpec, *, user, data):
    row = spec.model(**{name: data[name] for name in spec.fields if name in data})
    stamp_create(row, user)
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError as exc:
        raise _integrity_error(spec, exc) from None
    record(f"calculators.{spec.entity}_created", obj=row, actor=user, after=snapshot(row, spec.fields))
    bump(CACHE_NAMESPACE)
    return row


@transaction.atomic
def update_row(spec: SizingSpec, instance, *, user, data, expected_version=None):
    row = spec.model.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    before = snapshot(row, spec.fields)
    values = {name: data[name] for name in spec.fields if name in data and data[name] != getattr(row, name)}
    if not values:
        return row
    try:
        with transaction.atomic():
            row.versioned_update(user, **values)
    except IntegrityError as exc:
        raise _integrity_error(spec, exc) from None
    changed_before, changed_after = changes(before, snapshot(row, spec.fields))
    record(f"calculators.{spec.entity}_updated", obj=row, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return row


@transaction.atomic
def delete_row(spec: SizingSpec, instance, *, user, expected_version=None) -> None:
    row = spec.model.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    row.soft_delete(user)
    record(f"calculators.{spec.entity}_deleted", obj=row, actor=user, before=snapshot(row, spec.fields))
    bump(CACHE_NAMESPACE)


def services_for(spec: SizingSpec) -> dict:
    return {"create": partial(create_row, spec), "update": partial(update_row, spec), "destroy": partial(delete_row, spec)}
