"""Legacy import for the reference tables (PLAN §7.3 "``pincodes`` … ``ev_scooters`` → ``reference_*``: copy").

Every function takes plain row dicts exactly as the legacy tables hold them (``SELECT *``) and returns
``{"created", "updated", "skipped", "violations"}`` (``violations``: ``{"source_id", "field", "message"}``):

* each source row is tracked in ``core_legacy_map`` (``BACKEND`` × legacy table × id; appliances: ``FLARIZE`` ×
  ``quotation_content.appliances`` × appliance id), so a re-run updates changed rows and never duplicates; a row
  deleted in the platform after the import stays deleted (``skipped``);
* ``sort_order`` is the legacy id (the legacy APIs listed rows in insertion order), so the website order is kept;
* rows the database refuses (a duplicate device name, a negative price …) are skipped and reported;
* source timestamps are preserved where the legacy table had them;
* each call writes one ``audit_log`` row (``reference.legacy_imported``: counts + sha256 of the batch) and bumps the
  list's cache namespace.

Pincodes: the legacy table has one row per post office. Each office row becomes a ``reference_pincode_office``
(mapped); the ``reference_pincode`` row is created on first sight of the code and its ``district``/``state`` follow
the office with the lowest legacy id — what the legacy ``Pincode.objects.filter(pincode=…).first()`` returned.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from audit.services import record
from core.models import LegacyMap
from flarize.cache_utils import bump
from reference.models import Appliance, DeviceType, EvCar, EvScooter, KsebTariff, Pincode, PincodeOffice, RoomSize, Wattage
from reference.models.appliance import APPLIANCE_CODE_REGEX
from reference.models.pincode import PINCODE_REGEX
from reference.services.pincodes import CACHE_NAMESPACE as PINCODES_NAMESPACE

BACKEND = LegacyMap.SourceSystem.BACKEND
FLARIZE = LegacyMap.SourceSystem.FLARIZE


class Report:
    def __init__(self):
        self.created = self.updated = self.skipped = 0
        self.violations: list[dict] = []

    def violation(self, source_id, field: str, message: str) -> None:
        self.violations.append({"source_id": str(source_id), "field": field, "message": message})

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "violations": self.violations}


class Skip(Exception):
    """The row cannot be imported (the reason is already in the report)."""


def checksum(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, cls=DjangoJSONEncoder, sort_keys=True, default=str).encode()).hexdigest()


def _dt(value):
    if value in (None, ""):
        return None
    return parse_datetime(value) if isinstance(value, str) else value


def _decimal(value, source_id, field: str, report: Report) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        report.violation(source_id, field, f"not a number: {value!r}")
        raise Skip from None


def mapped_id(system: str, table: str, source_id) -> int | None:
    return LegacyMap.objects.filter(source_system=system, source_table=table, source_id=str(source_id)).values_list("target_id", flat=True).first()


def _upsert(model, *, system: str, table: str, source_id, values: dict, report: Report, created_at=None, updated_at=None):
    target_id = mapped_id(system, table, source_id)
    if target_id is not None:
        row = model.all_objects.filter(pk=target_id).first()
        if row is None or row.deleted_at is not None:
            report.skipped += 1
            return None
        changed = {name: value for name, value in values.items() if getattr(row, name) != value}
        if not changed:
            report.skipped += 1
            return row
        model.all_objects.filter(pk=row.pk).update(**changed, version=F("version") + 1, updated_at=updated_at or timezone.now())
        report.updated += 1
        return model.all_objects.get(pk=row.pk)
    row = model(**values)
    row.created_at = created_at or timezone.now()
    row.updated_at = updated_at or row.created_at
    model.objects.bulk_create([row])  # bypasses save(): source timestamps survive
    LegacyMap.objects.create(source_system=system, source_table=table, source_id=str(source_id), target_table=model._meta.db_table, target_id=row.pk)
    report.created += 1
    return row


def _run(rows: Iterable[dict], handle: Callable[[dict, Report], None], *, object_type: str, source: str, namespace: str, user, finish: Callable[[], None] | None = None) -> dict:
    rows = list(rows)
    report = Report()
    for row in rows:
        try:
            with transaction.atomic():
                handle(row, report)
        except Skip:
            continue
        except IntegrityError as exc:
            report.violation(row.get("id"), "row", f"rejected by the database: {str(exc).splitlines()[0]}")
    if finish is not None:
        finish()
    result = report.as_dict()
    record(
        "reference.legacy_imported",
        object_type=object_type,
        actor=user,
        actor_kind=None if user else "SYSTEM",
        after={
            "source": source,
            "rows": len(rows),
            "checksum": checksum(rows),
            "created": result["created"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "violations": len(result["violations"]),
        },
    )
    bump(namespace)
    return result


def _flat(model, table: str, namespace: str, object_type: str, transform: Callable[[dict, Report], dict], *, user, timestamps: bool = True):
    def run(rows: Iterable[dict]) -> dict:
        def handle(row: dict, report: Report) -> None:
            values = transform(row, report)
            values.setdefault("sort_order", int(row["id"]))
            created = _dt(row.get("created_at")) if timestamps else None
            updated = _dt(row.get("updated_at")) if timestamps else None
            _upsert(model, system=BACKEND, table=table, source_id=row["id"], values=values, report=report, created_at=created, updated_at=updated)

        return _run(rows, handle, object_type=object_type, source=f"BACKEND {table}", namespace=namespace, user=user)

    return run


# ----------------------------------------------------------------------------------------------------------------
# Flat lists
# ----------------------------------------------------------------------------------------------------------------
def import_tariffs(rows: Iterable[dict], *, user=None) -> dict:
    """``kseb_tariffs`` (``min_units``, ``max_units`` null = open-ended, ``rate``) → ``reference_kseb_tariff``."""

    def transform(row: dict, report: Report) -> dict:
        return {
            "slab_from_units": int(row["min_units"]),
            "slab_to_units": None if row.get("max_units") in (None, "") else int(row["max_units"]),
            "rate_per_unit": _decimal(row["rate"], row["id"], "rate", report),
            "phase": None,
            "effective_from": None,
        }

    return _flat(KsebTariff, "kseb_tariffs", "reference:tariffs", "reference.ksebtariff", transform, user=user)(rows)


def import_device_types(rows: Iterable[dict], *, user=None) -> dict:
    def transform(row: dict, report: Report) -> dict:
        name = (row.get("name") or "").strip()
        if not name:
            report.violation(row["id"], "name", "a device type needs a name")
            raise Skip
        return {"name": name, "show_in_ui": bool(row.get("show_in_ui", True)), "url": row.get("url") or "", "watts": row.get("watts"), "k_value": row.get("k_value")}

    return _flat(DeviceType, "device_types", "reference:device-types", "reference.devicetype", transform, user=user)(rows)


def import_wattages(rows: Iterable[dict], *, user=None) -> dict:
    def transform(row: dict, report: Report) -> dict:
        return {"value": int(row["value"]), "show_in_ui": bool(row.get("show_in_ui", True))}

    return _flat(Wattage, "wattages", "reference:wattages", "reference.wattage", transform, user=user)(rows)


def import_room_sizes(rows: Iterable[dict], *, user=None) -> dict:
    """``room_size`` has no timestamps; rows are stamped with the import time."""

    def transform(row: dict, report: Report) -> dict:
        return {"bhk_type": int(row["bhk_type"]), "size": int(row["size"]), "units": int(row["units"])}

    return _flat(RoomSize, "room_size", "reference:room-sizes", "reference.roomsize", transform, user=user, timestamps=False)(rows)


def _vehicle(row: dict, report: Report) -> dict:
    model = (row.get("model") or "").strip()
    if not model:
        report.violation(row["id"], "model", "a vehicle needs a model name")
        raise Skip
    return {
        "model": model,
        "battery_capacity": float(row["battery_capacity"]),
        "claimed_range": int(row["claimed_range"]),
        "adjusted_real_world_range": int(row["adjusted_real_world_range"]),
        "ex_showroom_price": _decimal(row["ex_showroom_price"], row["id"], "ex_showroom_price", report).quantize(Decimal("0.01")),
        "energy_consumption": None if row.get("energy_consumption") is None else float(row["energy_consumption"]),
        "k_value": None if row.get("k_value") is None else float(row["k_value"]),
    }


def import_ev_cars(rows: Iterable[dict], *, user=None) -> dict:
    return _flat(EvCar, "ev_cars", "reference:ev-cars", "reference.evcar", _vehicle, user=user)(rows)


def import_ev_scooters(rows: Iterable[dict], *, user=None) -> dict:
    return _flat(EvScooter, "ev_scooters", "reference:ev-scooters", "reference.evscooter", _vehicle, user=user)(rows)


def import_appliances(rows: Iterable[dict], *, user=None) -> dict:
    """Flarize ``quotation-content.json`` → ``appliances.master`` (``id``, ``name{en,ml}``, ``icon``, ``watts``,
    ``defaultHours``, ``optional``) → ``reference_appliance``; ``sort_order`` is the position in the master list."""
    rows = list(rows)
    positions = {str(row.get("id")): index for index, row in enumerate(rows, start=1)}

    def handle(row: dict, report: Report) -> None:
        code = str(row.get("id") or "")
        if not re.match(APPLIANCE_CODE_REGEX, code):
            report.violation(code, "id", "appliance ids are 2-32 characters of a-z, 0-9 and _")
            raise Skip
        name = row.get("name") or {}
        name = name if isinstance(name, dict) else {"en": str(name)}
        values = {
            "code": code,
            "name": (name.get("en") or code)[:100],
            "name_ml": (name.get("ml") or "")[:100],
            "icon": (row.get("icon") or "")[:16],
            "watts": int(row.get("watts") or 0),
            "default_hours": _decimal(row.get("defaultHours", 0), code, "defaultHours", report),
            "is_optional": bool(row.get("optional", False)),
            "sort_order": positions[code],
        }
        _upsert(Appliance, system=FLARIZE, table="quotation_content.appliances", source_id=code, values=values, report=report)

    return _run(rows, handle, object_type="reference.appliance", source="FLARIZE quotation-content.json appliances.master", namespace="reference:appliances", user=user)


# ----------------------------------------------------------------------------------------------------------------
# Pincodes (one legacy row per post office)
# ----------------------------------------------------------------------------------------------------------------
def import_pincodes(rows: Iterable[dict], *, user=None) -> dict:
    rows = list(rows)
    touched: set[int] = set()
    maps = dict(LegacyMap.objects.filter(source_system=BACKEND, source_table="pincodes").values_list("source_id", "target_id"))
    pincodes = {pincode.pincode: pincode for pincode in Pincode.objects.all()}

    def pincode_for(code: str, row: dict) -> Pincode:
        """The live pincode row; a new one is only remembered once its office row has been written."""
        pincode = pincodes.get(code)
        if pincode is None:
            created = _dt(row.get("created_at")) or timezone.now()
            pincode = Pincode(pincode=code, district=row.get("district") or "", state=row.get("state") or "", created_at=created, updated_at=created)
            Pincode.objects.bulk_create([pincode])
        return pincode

    def handle(row: dict, report: Report) -> None:
        code = str(row.get("pincode") or "").strip()
        if not re.match(PINCODE_REGEX, code):
            report.violation(row.get("id"), "pincode", f"not a 6-digit pincode: {code!r}")
            raise Skip
        target = maps.get(str(row["id"]))
        if target is not None:
            office = PincodeOffice.all_objects.filter(pk=target).select_related("pincode").first()
            if office is None or office.deleted_at is not None:
                report.skipped += 1
                return
            pincode = office.pincode if office.pincode.pincode == code else pincode_for(code, row)
        else:
            pincode = pincode_for(code, row)
        values = {
            "pincode": pincode,
            "office_name": (row.get("office_name") or "")[:100],
            "district": (row.get("district") or "")[:100],
            "state": (row.get("state") or "")[:100],
            "region": (row.get("region") or "")[:100],
            "division": (row.get("division") or "")[:100],
            "sort_order": int(row["id"]),
        }
        office = _upsert(
            PincodeOffice, system=BACKEND, table="pincodes", source_id=row["id"], values=values, report=report, created_at=_dt(row.get("created_at")), updated_at=_dt(row.get("updated_at"))
        )
        pincodes[code] = pincode
        if office is not None:
            touched.add(pincode.pk)

    def finish() -> None:
        for pincode in Pincode.objects.filter(pk__in=touched):
            first = PincodeOffice.objects.filter(pincode=pincode).order_by("sort_order", "id").first()
            if first is not None and (pincode.district, pincode.state) != (first.district, first.state):
                Pincode.objects.filter(pk=pincode.pk).update(district=first.district, state=first.state, version=F("version") + 1, updated_at=timezone.now())

    return _run(rows, handle, object_type="reference.pincode", source="BACKEND pincodes", namespace=PINCODES_NAMESPACE, user=user, finish=finish)
