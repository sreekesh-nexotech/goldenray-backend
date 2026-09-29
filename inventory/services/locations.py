"""``inventory/locations/`` — where stock is kept (module ``inventory``: view / edit).

* codes are unique among live locations, case-insensitive (409 ``location_code_taken``); ``code`` is 1-30 letters,
  digits, ``_``, ``.`` or ``-`` starting with a letter or digit;
* ``office`` (optional) is a live HR office, given by uid. inventory never imports hr: the office model is the one the
  ``Location.office`` relation points at. A location whose office was later soft-deleted reads as having no office
  (hr soft-deletes, so the PLAN's ``SET_NULL`` never fires);
* a location still holding stock (any non-zero balance) cannot be deleted (409 ``location_has_stock``); the delete is
  soft, its movements stay in the ledger;
* the location ``INVENTORY_RECEIVING_LOCATION`` names (by code, case-insensitive) can be neither deleted nor recoded
  (409 ``receiving_location``): every committed procurement batch would otherwise fail to book and be parked.
"""

from __future__ import annotations

import re
import uuid

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from core.errors import Conflict
from core.services import stamp_create
from inventory.models import Balance, Location
from inventory.models.location import CODE_PATTERN
from inventory.services.common import bump_locations, lock, receiving_location_code, unique_conflict, validation_error

EDITABLE_FIELDS = ("code", "name", "office")
SNAPSHOT_FIELDS = EDITABLE_FIELDS
UNIQUE = {"inventory_location_code_live_uniq": ("location_code_taken", "code", "Another location already uses this code.")}
_CODE_RE = re.compile(CODE_PATTERN)


def office_model():
    """``hr.Office`` without importing hr (the model the ``Location.office`` relation points at)."""
    return Location._meta.get_field("office").related_model


def live_office(location: Location):
    """The location's office, or ``None`` when it has none or the office was soft-deleted."""
    office = location.office if location.office_id else None
    return office if office is not None and office.deleted_at is None else None


def locations_queryset():
    return Location.objects.select_related("office").order_by("code", "id")


def find_by_code(code: str) -> Location | None:
    code = (code or "").strip()
    return Location.objects.filter(code__iexact=code).first() if code else None


def _resolve_office(value):
    if value is None or value == "":
        return None
    model = office_model()
    if isinstance(value, model):
        office = value if value.deleted_at is None else None
    else:
        try:
            uid = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            raise validation_error({"office": ["Must be a valid UUID."]}) from None
        office = model.objects.filter(uid=uid).first()  # live offices only (BaseModel.objects)
    if office is None:
        raise validation_error({"office": ["Unknown office."]})
    return office


def _normalise(data) -> dict:
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data}
    errors = {}
    if "code" in values:
        values["code"] = (values["code"] or "").strip()
        if not _CODE_RE.match(values["code"]):
            errors["code"] = ["1-30 letters, digits, '_', '.' or '-', starting with a letter or digit."]
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
        if not values["name"]:
            errors["name"] = ["This field may not be blank."]
    if errors:
        raise validation_error(errors)
    if "office" in values:
        values["office"] = _resolve_office(values["office"])
    return values


def _save(action) -> None:
    try:
        with transaction.atomic():
            action()
    except IntegrityError as exc:
        raise (unique_conflict(exc, UNIQUE) or exc) from None


def location_snapshot(location: Location) -> dict:
    return snapshot(location, SNAPSHOT_FIELDS)


def _refuse_if_receiving(location: Location, field: str, what: str) -> None:
    """``INVENTORY_RECEIVING_LOCATION`` names a location by code: recoding or deleting it would make every
    ``procurement.batch_committed`` receipt fail (retried, then parked), so the setting has to change first."""
    code = receiving_location_code()
    if code and location.code.upper() == code.upper():
        raise Conflict(
            "receiving_location",
            f"'{location.code}' receives committed procurement batches (INVENTORY_RECEIVING_LOCATION); change that setting before {what} it.",
            errors={field: ["The receiving location."]},
        )


@transaction.atomic
def create_location(*, user, data) -> Location:
    values = _normalise(data)
    missing = {name: ["This field is required."] for name in ("code", "name") if name not in values}
    if missing:
        raise validation_error(missing)
    location = Location(**values)
    stamp_create(location, user)
    _save(location.save)
    record("inventory.location_created", obj=location, actor=user, after=location_snapshot(location))
    bump_locations()
    return location


@transaction.atomic
def update_location(instance: Location, *, user, data, expected_version=None) -> Location:
    location = lock(Location, instance, expected_version)
    before = location_snapshot(location)
    values = _normalise(data)
    values = {name: value for name, value in values.items() if getattr(location, name) != value}
    if not values:
        return location
    if "code" in values and values["code"].upper() != location.code.upper():  # the setting is matched case-insensitively
        _refuse_if_receiving(location, "code", "recoding")
    _save(lambda: location.versioned_update(user, **values))
    changed_before, changed_after = changes(before, location_snapshot(location))
    record("inventory.location_updated", obj=location, actor=user, before=changed_before, after=changed_after)
    bump_locations()
    return location


@transaction.atomic
def delete_location(instance: Location, *, user, expected_version=None) -> None:
    # The row lock also serialises with movements, which lock the location before they read its balance.
    location = lock(Location, instance, expected_version)
    _refuse_if_receiving(location, "location", "deleting")
    stocked = Balance.objects.filter(location=location).exclude(qty=0).count()
    if stocked:
        raise Conflict(
            "location_has_stock",
            f"'{location.code}' still holds stock of {stocked} component(s); move or adjust it to zero first.",
            errors={"stock": [f"{stocked} component(s) with a non-zero balance."]},
        )
    location.soft_delete(user)
    record("inventory.location_deleted", obj=location, actor=user, before=location_snapshot(location))
    bump_locations()
