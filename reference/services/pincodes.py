"""Staff writes for ``reference/pincodes/`` (module ``reference_data``) — a pincode and its post offices.

``offices`` is a nested list on create/update: items carrying a ``uid`` update that office, items without one are
created, and live offices missing from the list are removed (soft delete). Omitting ``offices`` on update leaves
them alone. ``district``/``state`` default to the first office's values. A live pincode is unique (409
``pincode_exists``). Writes are audited and bump ``reference:pincodes``.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from core.errors import DomainError
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from reference.models import Pincode, PincodeOffice
from reference.services.lists import integrity_error

CACHE_NAMESPACE = "reference:pincodes"
FIELDS = ("pincode", "district", "state", "serviceable", "distance_km_from_office", "is_active", "sort_order")
OFFICE_FIELDS = ("office_name", "district", "state", "region", "division", "sort_order")


def pincodes_queryset():
    return Pincode.objects.prefetch_related("offices").order_by("pincode", "id")


def pincode_snapshot(pincode: Pincode) -> dict:
    data = snapshot(pincode, FIELDS)
    data["offices"] = [office.office_name for office in PincodeOffice.objects.filter(pincode=pincode).order_by("sort_order", "id")]
    return data


def _defaults_from_offices(values: dict, offices: list[dict] | None) -> None:
    if offices:
        for name in ("district", "state"):
            if not values.get(name):
                values[name] = offices[0].get(name, "")


def _write_offices(pincode: Pincode, offices: list[dict], *, user) -> None:
    existing = {office.uid: office for office in PincodeOffice.objects.select_for_update().filter(pincode=pincode)}
    keep = set()
    for position, item in enumerate(offices):
        values = {name: item[name] for name in OFFICE_FIELDS if name in item}
        values.setdefault("sort_order", position)
        uid = item.get("uid")
        if uid is not None:
            office = existing.get(uid)
            if office is None:
                raise DomainError("validation_error", "Unknown office.", errors={"offices": [f"Office {uid} does not belong to this pincode."]})
            keep.add(uid)
            changed = {name: value for name, value in values.items() if getattr(office, name) != value}
            if changed:
                office.versioned_update(user, **changed)
            continue
        office = PincodeOffice(pincode=pincode, **values)
        stamp_create(office, user)
        office.save()
    for uid, office in existing.items():
        if uid not in keep:
            office.soft_delete(user)


@transaction.atomic
def create_pincode(*, user, data) -> Pincode:
    values = {name: data[name] for name in FIELDS if name in data}
    offices = data.get("offices") or []
    _defaults_from_offices(values, offices)
    pincode = Pincode(**values)
    stamp_create(pincode, user)
    try:
        with transaction.atomic():
            pincode.save()
    except IntegrityError as exc:
        raise integrity_error("pincode", "pincode", exc) from None
    _write_offices(pincode, offices, user=user)
    record("reference.pincode_created", obj=pincode, actor=user, after=pincode_snapshot(pincode))
    bump(CACHE_NAMESPACE)
    return pincode


@transaction.atomic
def update_pincode(instance: Pincode, *, user, data, expected_version=None) -> Pincode:
    pincode = Pincode.objects.select_for_update().get(pk=instance.pk)
    check_version(pincode, expected_version)
    before = pincode_snapshot(pincode)
    values = {name: data[name] for name in FIELDS if name in data and data[name] != getattr(pincode, name)}
    offices = data.get("offices")
    if not values and offices is None:
        return pincode
    try:
        with transaction.atomic():
            if values:
                pincode.versioned_update(user, **values)
            if offices is not None:
                _write_offices(pincode, offices, user=user)
                if not values:
                    pincode.versioned_update(user)  # an office edit is an edit of the pincode (version, updated_at)
    except IntegrityError as exc:
        raise integrity_error("pincode", "pincode", exc) from None
    changed_before, changed_after = changes(before, pincode_snapshot(pincode))
    if changed_before or changed_after:
        record("reference.pincode_updated", obj=pincode, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return pincode


@transaction.atomic
def delete_pincode(instance: Pincode, *, user, expected_version=None) -> None:
    pincode = Pincode.objects.select_for_update().get(pk=instance.pk)
    check_version(pincode, expected_version)
    pincode.soft_delete(user)
    record("reference.pincode_deleted", obj=pincode, actor=user, before=pincode_snapshot(pincode))
    bump(CACHE_NAMESPACE)
