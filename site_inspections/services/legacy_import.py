"""Site Inspection V2 (SQLite) → platform (PLAN §7.5; Plan 2 §3.2 "Migration").

Every function takes the rows of one legacy table as plain dicts (``SELECT *``) and returns ``{"created", "updated",
"skipped", "violations"}``; each is idempotent through ``core_legacy_map`` (source system ``SI``, the legacy table name,
the legacy id): re-running updates what changed and never duplicates. ``dry_run=True`` rolls everything back (photos
are then not stored either). Order (:func:`import_all`): engineers → customers → inspections → photos → annotations →
equipment → approvals → observations, then the layout photo references.

Rules (column by column, see ``docs/decisions/site-inspections.md`` → *Legacy mapping*):

* customers are matched **by phone only** (``customers.services.legacy_import.match_or_create_by_phone``; unmatched →
  new, ``source=SI_IMPORT``); engineers become users with the Field Engineer role, no password (a reset link is sent
  by the operator; scrypt hashes are not portable);
* type coercion: ``"No"``/``"NO"``/``0`` → false, ``"Yes"``/``"YES"``/``1`` → true (legacy ``asBoolean``); enums are
  validated (unknown → blank + ``invalid_enum`` violation); vehicle-type labels → codes; free-text phase → 1P/3P/NC;
  numbers from TEXT columns parsed; ``system_type`` from the column, else recomputed from the snapshot;
* suitability ← ``normalise_suitability(final_recommendation, site_suitable_for_solar)`` (CONDITIONAL is kept);
  neutral link and termination point are each normalised (the legacy stored one collapsed value in both);
* the three commercial columns are **not imported** (reported once per inspection that had them);
* the V2 additional-work flags become ``site_inspections_additional_work_item`` rows (``PARTIAL`` underground cabling
  counts as required);
* photos: base64 data URLs → private media (sniffed like any upload); rectangles are imported with
  ``geometry_space=LEGACY_CONTAINER`` (they must be re-drawn before release);
* equipment status is recomputed over the full check definition; every change against the stored legacy status is a
  ``status_recomputed`` violation (the diff report for the Project Head);
* approvals keep their number/status with ``otp_verified_at`` null and no location snapshot; a legacy PENDING approval
  (no link was ever sent) is imported as EXPIRED.

The agreement reference of a legacy PA inspection is ``uuid5(SI_AGREEMENT_NAMESPACE, "PA:<legacy agreement id>")`` —
the agreements importer gives its imported PA rows the same uid, so the link holds without a lookup.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from decimal import Decimal, InvalidOperation

from accounts.models import Role, User
from core.errors import DomainError
from core.models import LegacyMap
from core.sequences import ensure_next_value_at_least
from customers.models import Customer
from customers.services import legacy_import as customer_import
from customers.services.import_support import ImportRun, date_value, run_import, timestamp, upsert
from customers.services.phones import try_normalise
from engines import inspection_checks as checks
from engines import inspection_readiness as ir
from media.models import MediaAsset
from media.services import assets
from site_inspections.models import AdditionalWorkItem, Annotation, EquipmentAssessment, Inspection, LocationApproval, Observation, Photo, Snapshot
from site_inspections.models import choices as C
from site_inspections.services.common import CACHE_NAMESPACE, OBJECT_TYPE
from site_inspections.services.photos import folder_for

SI = LegacyMap.SourceSystem.SI
ACTION = "site_inspections.legacy_imported"
SI_AGREEMENT_NAMESPACE = uuid.UUID("5b0c1a6e-7f53-4d0e-9a51-5f4c2a3e9d11")
ENGINEER_ROLE = "field-engineer"
COMMERCIAL = ("quoted_original_price", "quoted_discount", "quoted_final_price")
VEHICLE = {"car": "CAR", "pickup / van": "PICKUP_VAN", "pickup_van": "PICKUP_VAN", "truck": "TRUCK", "manual movement only": "MANUAL", "manual": "MANUAL", "not assessed": "NOT_ASSESSED"}
PHOTO_TYPE = {"SITE_ACCESS": "ACCESS", "EQUIPMENT": "EQUIPMENT_AREA", "CABLE_ROUTING": "CABLE_ROUTE"}
REVIEW = {"RESOLVED": "RESOLVED", "WAIVED_APPROVED": "WAIVED"}
WORK_FLAGS = {
    "walkway_required": ("WALKWAY", "walkway_length"),
    "ladder_required": ("LADDER", "ladder_length"),
    "sliding_door_required": ("SLIDING_DOOR", None),
    "underground_cabling": ("UNDERGROUND_CABLING", "ug_cable_length"),
    "extra_ac_cable": ("EXTRA_AC_CABLE", "extra_ac_cable_length"),
    "extra_dc_cable": ("EXTRA_DC_CABLE", "extra_dc_cable_length"),
    "new_neutral_link": ("NEW_NEUTRAL_LINK", None),
    "new_termination": ("NEW_TERMINATION", None),
    "additional_earthing_required": ("ADDITIONAL_EARTHING", None),
    "civil_work": ("CIVIL_WORK", None),
    "additional_structure_work": ("STRUCTURE_CHANGE", None),
}
ENUMS = {
    "road_access": C.RoadAccess,
    "roof_type": C.RoofType,
    "roof_strength": C.RoofStrength,
    "roof_accessibility": C.RoofAccessibility,
    "roof_condition": C.RoofCondition,
    "installation_difficulty": C.Level,
    "roof_slope": C.RoofSlope,
    "direction_facing": C.Direction,
    "morning_shading": C.Shading,
    "afternoon_shading": C.Shading,
    "generation_impact": C.GenerationImpact,
    "tariff": C.Tariff,
    "distribution_board": C.Availability,
    "earthing": C.Availability,
    "site_structure_type": C.StructureDecision,
}
TEXT = {
    "address": 10_000,
    "location": 120,
    "district": 100,
    "access_remarks": 10_000,
    "building_type": 60,
    "roof_accessibility_reason": 10_000,
    "shade_source": 255,
    "shading_remarks": 10_000,
    "consumer_name": 255,
    "meter_number": 40,
    "electrical_remarks": 10_000,
    "battery_cable_route": 10_000,
    "cable_routing_remarks": 10_000,
    "final_recommendation": 10_000,
    "engineer_remarks": 10_000,
    "additional_requirements": 10_000,
    "approval_remarks": 10_000,
    "customer_restrictions": 10_000,
    "customer_location_remarks": 10_000,
    "complexity_reason": 10_000,
    "quoted_structure_type": 32,
    "quoted_structure_material": 64,
}
NUMBERS = {
    "location_accuracy": "location_accuracy_m",
    "roof_length": "roof_length_m",
    "roof_width": "roof_width_m",
    "available_area": "available_area_m2",
    "usable_area": "usable_area_m2",
    "connected_load": "connected_load_kw",
    "sanctioned_load": "sanctioned_load_kw",
    "ac_cable_measurement": "ac_cable_m",
    "dc_cable_measurement": "dc_cable_m",
    "la_cable_measurement": "la_cable_m",
    "battery_cable_length": "battery_cable_m",
    "quoted_solar_size": "quoted_size_kw",
    "walkway_length": "walkway_length_m",
    "walkway_width": "walkway_width_m",
    "ladder_length": "ladder_length_m",
    "sliding_door_width": "sliding_door_width_m",
    "sliding_door_height": "sliding_door_height_m",
    "elevated_height": "elevated_height_m",
    "panel_width": "panel_width_m",
    "panel_height": "panel_height_m",
    "panel_area": "panel_area_m2",
    "equipment_width": "equipment_width_m",
    "equipment_height": "equipment_height_m",
    "equipment_area": "equipment_area_m2",
}
PERCENT = {"shading_percentage": "shading_pct", "suitability_percentage": "suitability_pct"}


# ── value coercion ──────────────────────────────────────────────────────────────────────────────────────────────────


def legacy_bool(value) -> bool:
    """The legacy ``asBoolean`` (true for true, 1, "1", "true", "yes"), plus ``PARTIAL`` (partial work is work)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value or "").strip().lower() in ("1", "true", "yes", "partial")


def _decimal(value, places: int = 2) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(re.sub(r"[^0-9.\-]", "", str(value)) or "x")
    except InvalidOperation:
        return None
    return number.quantize(Decimal(1).scaleb(-places)) if number.is_finite() and number >= 0 else None


def _coordinate(run: ImportRun, row_id, column: str, value, limit: int) -> Decimal | None:
    """A REAL column may hold text in SQLite ("" from an emptied form): anything that is not a number in range is
    left blank and reported — one bad row must never abort the import."""
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        number = None
    if number is None or not number.is_finite() or not -limit <= number <= limit:
        run.violation(row_id, "out_of_range", f"{column}={value!r} is not a coordinate; left blank.")
        return None
    return number.quantize(Decimal("0.000001"))


def _enum(run: ImportRun, row_id, column: str, value, kind) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    if text in kind.values:
        return text
    run.violation(row_id, "invalid_enum", f"{column}={value!r} is not one of {', '.join(kind.values)}; left blank.")
    return ""


def _phase(run: ImportRun, row_id, value) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "three" in text or text.startswith("3"):
        return C.Phase.THREE
    if "single" in text or text.startswith("1") or text == "one":
        return C.Phase.SINGLE
    if "not" in text:
        return C.Phase.NOT_CONFIRMED
    run.violation(row_id, "invalid_enum", f"phase={value!r} not recognised; left blank.")
    return ""


def _mapped(source_table: str, source_id) -> int | None:
    return LegacyMap.objects.filter(source_system=SI, source_table=source_table, source_id=str(source_id)).values_list("target_id", flat=True).first() if source_id else None


def agreement_uid(legacy_agreement_id) -> uuid.UUID | None:
    return uuid.uuid5(SI_AGREEMENT_NAMESPACE, f"PA:{legacy_agreement_id}") if legacy_agreement_id else None


# ── engineers & customers ───────────────────────────────────────────────────────────────────────────────────────────


def _import_engineer(run: ImportRun, row: dict, logins: dict) -> None:
    role = Role.objects.filter(slug=ENGINEER_ROLE).first()
    if role is None:
        run.violation(row["id"], "role_missing", f"Role {ENGINEER_ROLE!r} is not seeded; run seed_roles first.")
        run.skipped += 1
        return
    login = logins.get(row["id"]) or {}
    active = row.get("status") == "active" and bool(login.get("active", 1))
    names = (row.get("name") or row["engineer_code"]).split(" ", 1)
    values = {"first_name": names[0][:150], "last_name": (names[1] if len(names) > 1 else "")[:150], "is_active": active}
    pk = _mapped("engineers", row["id"])
    user = User.all_objects.filter(pk=pk).first() if pk else None
    if user is None:
        email = f"{re.sub(r'[^a-z0-9]+', '-', row['engineer_code'].lower()).strip('-')}@site-engineers.invalid"
        user = User.objects.filter(email=email).first() or User.objects.create_user(email, role=role, must_reset_password=True, **values)
        run.violation(row["id"], "email_placeholder", f"The legacy engineer has no e-mail; {email} was set — replace it before sending the reset link.")
        run.created += 1
    elif any(getattr(user, key) != value for key, value in values.items()):
        User.all_objects.filter(pk=user.pk).update(**values)
        run.updated += 1
    else:
        run.skipped += 1
    run.link(row["id"], user)


def import_engineers(rows: list[dict], *, logins: list[dict] = (), user=None, dry_run: bool = False) -> dict:
    by_engineer = {login["engineer_id"]: login for login in logins}
    return run_import(ImportRun(SI, "engineers"), rows, lambda run, row: _import_engineer(run, row, by_engineer), user=user, dry_run=dry_run, action=ACTION, object_type=OBJECT_TYPE, namespaces=())


def _import_customer(run: ImportRun, row: dict) -> None:
    values = {key: (row.get(key) or "")[:limit] for key, limit in (("email", 254), ("address", 10_000), ("location", 120), ("district", 100)) if row.get(key)}
    if row.get("pincode") and re.fullmatch(r"[1-9][0-9]{5}", str(row["pincode"])):
        values["pincode"] = str(row["pincode"])
    pk = _mapped("customers", row["id"])
    if pk and Customer.all_objects.filter(pk=pk).exists():
        run.skipped += 1
        return
    customer, created = customer_import.match_or_create_by_phone(phone=row.get("phone"), name=row.get("name") or "", source=Customer.Source.SI_IMPORT, values=values)
    if customer is None:
        run.violation(row["id"], "unparsable_phone", f"Customer {row.get('customer_code')} has no usable phone ({row.get('phone')!r}); not imported, its inspections are skipped.")
        run.skipped += 1
        return
    run.created += int(created)
    run.skipped += int(not created)
    if not created:
        run.violation(row["id"], "matched_by_phone", f"Linked to the existing customer {customer.code}.")
    run.link(row["id"], customer)


def import_customers(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import(ImportRun(SI, "customers"), rows, _import_customer, user=user, dry_run=dry_run, action=ACTION, object_type=OBJECT_TYPE, namespaces=())


# ── inspections ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _system_type(row: dict) -> str:
    column = str(row.get("system_type") or "").upper()
    if column in (C.SystemType.ON_GRID, C.SystemType.HYBRID):
        return column
    try:
        snapshot = json.loads(row.get("inspection_snapshot_json") or "null") or {}
    except ValueError:
        snapshot = {}
    derived = str(snapshot.get("systemType") or (snapshot.get("system") or {}).get("type") or "").upper()
    return derived if derived in (C.SystemType.ON_GRID, C.SystemType.HYBRID) else C.SystemType.UNDECIDED


def _inspection_values(run: ImportRun, row: dict) -> dict | None:
    rid = row["id"]
    customer_id = _mapped("customers", row.get("customer_id"))
    if customer_id is None:
        run.violation(rid, "customer_unmapped", f"Customer {row.get('customer_id')!r} was not imported; inspection skipped.")
        return None
    status = str(row.get("status") or "DRAFT").upper()
    if status not in C.Status.values:
        run.violation(rid, "invalid_enum", f"status={row.get('status')!r}; imported as DRAFT.")
        status = C.Status.DRAFT
    agreement = agreement_uid(row.get("purchase_agreement_id"))
    values = {
        "number": row["site_visit_no"],
        "visit_date": date_value(row.get("site_visit_date")),
        "customer_id": customer_id,
        "engineer_id": _mapped("engineers", row.get("engineer_id")),
        "origin": C.Origin.AGREEMENT if agreement else C.Origin.PRE_SALE,
        "system_type": _system_type(row),
        "status": status,
        "complexity_status": _enum(run, rid, "complexity_status", row.get("complexity_status"), C.Complexity) or C.Complexity.NOT_ASSESSED,
        "agreement_uid": agreement,
        "agreement_version": row.get("purchase_agreement_version") if agreement else None,
        "pincode": str(row.get("pincode") or "") if re.fullmatch(r"[1-9][0-9]{5}", str(row.get("pincode") or "")) else "",
        "google_map_link": (row.get("google_map_link") or "")[:500],
        "latitude": _coordinate(run, rid, "latitude", row.get("latitude"), 90),
        "longitude": _coordinate(run, rid, "longitude", row.get("longitude"), 180),
        "location_captured_at": timestamp(row.get("location_captured_at")),
        "vehicle_type": VEHICLE.get(str(row.get("vehicle_type") or "").strip().lower(), ""),
        "no_of_floors": int(row["no_of_floors"]) if str(row.get("no_of_floors") or "").isdigit() else None,
        "consumer_number": str(row.get("consumer_number") or "")[:20],
        "registered_phone_e164": try_normalise(row.get("registered_phone")) or "" if row.get("registered_phone") else "",
        "phase": _phase(run, rid, row.get("phase")),
        "neutral_link": ir.normalise_availability(row.get("neutral_link")) or "",
        "termination_point": ir.normalise_availability(row.get("termination_point")) or "",
        "wheeling_required": None if row.get("wheeling_required") in (None, "") else legacy_bool(row.get("wheeling_required")),
        "site_suitability": ir.normalise_suitability(row.get("final_recommendation"), row.get("site_suitable_for_solar")) or "",
        "has_location_restrictions": legacy_bool(row.get("has_location_restrictions")),
        "quoted_panel_capacity_w": int(match.group(1)) if (match := re.search(r"(\d{3,4})", str(row.get("quoted_panel_capacity") or ""))) else None,
        "walkway_required": legacy_bool(row.get("walkway_required")),
        "ladder_required": legacy_bool(row.get("ladder_required")),
        "sliding_door_required": legacy_bool(row.get("sliding_door_required")),
    }
    if row.get("vehicle_type") and not values["vehicle_type"]:
        run.violation(rid, "invalid_enum", f"vehicle_type={row['vehicle_type']!r} not recognised; left blank.")
    if row.get("registered_phone") and not values["registered_phone_e164"]:
        run.violation(rid, "unparsable_phone", f"registered_phone={row['registered_phone']!r} is not a phone number; left blank.")
    values.update({name: _enum(run, rid, name, row.get(name), kind) for name, kind in ENUMS.items()})
    values.update({name: str(row.get(name) or "")[:limit] for name, limit in TEXT.items()})
    values.update({target: _decimal(row.get(source)) for source, target in NUMBERS.items()})
    values["quoted_size_kw"] = _decimal(row.get("quoted_solar_size"), 3)
    for source, target in PERCENT.items():
        number = _decimal(row.get(source))
        if number is not None and number > 100:
            run.violation(rid, "out_of_range", f"{source}={row[source]!r} is above 100; left blank.")
            number = None
        values[target] = number
    if status == C.Status.ON_HOLD:
        values.update(on_hold_reason="Imported on hold from Site Inspection V2.", held_from_status=C.Status.DRAFT)
    if status == C.Status.INSTALLATION_READY:
        values["released_at"] = timestamp(row.get("approved_at")) or timestamp(row.get("updated_at"))
    if any(row.get(name) not in (None, "") for name in COMMERCIAL):
        run.violation(rid, "commercial_not_imported", "quoted prices are not imported (prices live on the agreement).")
    return values


def _work_items(run: ImportRun, inspection: Inspection, row: dict) -> None:
    status = str(row.get("additional_work_status") or "NONE").upper()
    status = status if status in C.WorkStatus.values and status != C.WorkStatus.NONE else C.WorkStatus.IDENTIFIED
    items = [(work_type, _decimal(row.get(length)) if length else None) for column, (work_type, length) in WORK_FLAGS.items() if legacy_bool(row.get(column))]
    if ir.has_value(row.get("other_additional_work")):
        items.append(("OTHER", None))
    for work_type, quantity in items:
        existing = AdditionalWorkItem.all_objects.filter(inspection=inspection, work_type=work_type).first()
        values = {
            "inspection_id": inspection.pk,
            "work_type": work_type,
            "required": True,
            "status": status,
            "quantity": quantity if quantity is not None else _decimal(row.get("additional_work_quantity")),
            "unit": C.WorkUnit.M if quantity is not None else (row.get("additional_work_unit") if row.get("additional_work_unit") in C.WorkUnit.values else ""),
            "dimensions": str(row.get("additional_work_dimensions") or "")[:120],
            "reason": "\n".join(
                str(row[key])
                for key in ("additional_work_reason", "additional_work_remarks", "other_additional_work")
                if row.get(key) and not (key == "other_additional_work" and work_type != "OTHER")
            ),
        }
        if status in (C.WorkStatus.APPROVED, C.WorkStatus.REJECTED):
            values["decided_at"] = timestamp(row.get("updated_at"))
        if status in (C.WorkStatus.COST_CALCULATED, C.WorkStatus.CUSTOMER_QUOTE_SENT):
            values["status"] = C.WorkStatus.ENGINEERING_REVIEW
            run.violation(row["id"], "work_status_downgraded", f"{work_type}: {status} needs an EXTRA_STRUCTURE agreement; imported as ENGINEERING_REVIEW.")
        if existing is None:
            AdditionalWorkItem.objects.create(**values)
        elif any(getattr(existing, key if not key.endswith("_id") else key) != value for key, value in values.items()):
            AdditionalWorkItem.all_objects.filter(pk=existing.pk).update(**values)


def _import_inspection(run: ImportRun, row: dict) -> None:
    values = _inspection_values(run, row)
    if values is None:
        run.skipped += 1
        return
    target = run.find_target(Inspection, row["id"], lambda: Inspection.all_objects.filter(number=row["site_visit_no"]).first())
    created_at = timestamp(row.get("created_at"))
    inspection = upsert(run, Inspection, row["id"], target=target, values=values, created_at=created_at, updated_at=timestamp(row.get("updated_at")) or created_at)
    if row.get("inspection_snapshot_json") and not Snapshot.all_objects.filter(inspection=inspection).exists():
        data = json.loads(row["inspection_snapshot_json"])
        source = C.SnapshotSource.AGREEMENT if inspection.origin == C.Origin.AGREEMENT else C.SnapshotSource.PRE_SALE
        Snapshot.objects.create(inspection=inspection, number=1, source=source, agreement_version=inspection.agreement_version, data={"legacy": data})
    _work_items(run, inspection, row)
    match = re.fullmatch(r"SV-(\d{8})-(\d+)", inspection.number)
    if match:
        ensure_next_value_at_least("SV", int(match.group(2)) + 1, period_key=match.group(1))


def import_inspections(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import(ImportRun(SI, "site_inspections"), rows, _import_inspection, user=user, dry_run=dry_run, action=ACTION, object_type=OBJECT_TYPE, namespaces=(CACHE_NAMESPACE,))


def link_layout(rows: list[dict]) -> None:
    """The inspections' reference photos, once the photos exist."""
    for row in rows:
        pk = _mapped("site_inspections", row["id"])
        if pk:
            Inspection.all_objects.filter(pk=pk).update(
                panel_photo_id=_mapped("site_inspection_photos", row.get("panel_photo_id")), equipment_photo_id=_mapped("site_inspection_photos", row.get("equipment_photo_id"))
            )


# ── photos, annotations, equipment, approvals, observations ─────────────────────────────────────────────────────────


class _Bytes:
    def __init__(self, data: bytes, name: str):
        import io

        self._buffer = io.BytesIO(data)
        self.name = name
        self.size = len(data)

    def __getattr__(self, attr):
        return getattr(self._buffer, attr)


def _decode(data_url: str) -> bytes | None:
    match = re.fullmatch(r"data:[\w/+.-]+;base64,(.+)", str(data_url or ""), re.S)
    if not match:
        return None
    try:
        return base64.b64decode(match.group(1), validate=True)
    except (binascii.Error, ValueError):
        return None


def _import_photo(run: ImportRun, row: dict, dry_run: bool) -> None:
    inspection = Inspection.all_objects.filter(pk=_mapped("site_inspections", row.get("inspection_id"))).first()
    if inspection is None:
        run.violation(row["id"], "inspection_unmapped", "The inspection was not imported; photo skipped.")
        run.skipped += 1
        return
    if _mapped("site_inspection_photos", row["id"]):
        run.skipped += 1  # photos are immutable in V2
        return
    data = _decode(row.get("file_url"))
    if data is None:
        run.violation(row["id"], "invalid_data_url", "file_url is not a base64 data URL; photo skipped.")
        run.skipped += 1
        return
    if dry_run:
        run.created += 1
        return
    try:
        asset = assets.upload(
            user=None,
            file=_Bytes(data, row.get("original_filename") or "photo.jpg"),
            visibility=MediaAsset.Visibility.PRIVATE,
            kind=MediaAsset.Kind.PHOTO,
            folder=folder_for(inspection),
            allow_reserved=True,
        )
    except DomainError as exc:
        run.violation(row["id"], "invalid_image", f"{exc.code}: {exc.message}; photo skipped.")
        run.skipped += 1
        return
    photo_type = PHOTO_TYPE.get(str(row.get("photo_type") or "").upper(), str(row.get("photo_type") or "").upper())
    photo = Photo.objects.create(
        inspection=inspection, asset=asset, photo_type=photo_type if photo_type in C.PhotoType.values else C.PhotoType.OTHER, captured_at=timestamp(row.get("captured_at")) or asset.captured_at
    )
    Photo.all_objects.filter(pk=photo.pk).update(created_at=timestamp(row.get("created_at")) or photo.created_at)
    run.created += 1
    run.link(row["id"], photo)


def import_photos(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import(
        ImportRun(SI, "site_inspection_photos"),
        rows,
        lambda run, row: _import_photo(run, row, dry_run),
        user=user,
        dry_run=dry_run,
        action=ACTION,
        object_type=OBJECT_TYPE,
        namespaces=(CACHE_NAMESPACE,),
    )


def _import_annotation(run: ImportRun, row: dict) -> None:
    inspection_id = _mapped("site_inspections", row.get("inspection_id"))
    photo = Photo.all_objects.filter(pk=_mapped("site_inspection_photos", row.get("photo_id")), inspection_id=inspection_id).first() if inspection_id else None
    kind = str(row.get("annotation_type") or "").upper()
    if photo is None or kind not in C.AnnotationType.values:
        run.violation(row["id"], "annotation_unusable", "No imported photo of the same inspection (or unknown type); rectangle skipped — re-draw it.")
        run.skipped += 1
        return
    geometry = json.loads(row.get("geometry_json") or "{}")
    meta = json.loads(row.get("metadata_json") or "{}") or {}
    width, height = _decimal(meta.get("widthM")), _decimal(meta.get("heightM"))
    values = {
        "inspection_id": inspection_id,
        "photo_id": photo.pk,
        "annotation_type": kind,
        "geometry": {"x": geometry.get("x"), "y": geometry.get("y"), "w": geometry.get("width", geometry.get("w")), "h": geometry.get("height", geometry.get("h"))},
        "geometry_space": C.GeometrySpace.LEGACY_CONTAINER,
        "width_m": width if width else None,
        "height_m": height if height else None,
        "area_m2": _decimal(meta.get("areaM2")),
        "number": 1,
        "is_current": True,
    }
    target = run.find_target(Annotation, row["id"])
    upsert(run, Annotation, row["id"], target=target, values=values, created_at=timestamp(row.get("created_at")), updated_at=timestamp(row.get("updated_at")))


def import_annotations(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import(ImportRun(SI, "site_inspection_annotations"), rows, _import_annotation, user=user, dry_run=dry_run, action=ACTION, object_type=OBJECT_TYPE, namespaces=(CACHE_NAMESPACE,))


def _import_equipment(run: ImportRun, row: dict) -> None:
    inspection_id = _mapped("site_inspections", row.get("inspection_id"))
    kind = str(row.get("equipment_type") or "").upper()
    if inspection_id is None or kind not in C.EquipmentType.values:
        run.violation(row["id"], "equipment_unusable", "Inspection not imported or unknown equipment type; skipped.")
        run.skipped += 1
        return
    raw = json.loads(row.get("results_json") or "{}") or {}
    errors = checks.validate_results(kind, raw)
    if errors:
        run.violation(row["id"], "invalid_results", f"Dropped unknown checks/results: {errors}.")
    clean = {key: value for key, value in raw.items() if key not in errors}
    results = {key: str(value) for key, value in checks.normalise_results(kind, clean).items()}
    status = str(checks.compute_status(kind, results))
    if row.get("status") != status:
        run.violation(row["id"], "status_recomputed", f"{kind}: legacy {row.get('status')} → {status} (full check definition).")
    review = REVIEW.get(str(row.get("resolution_status") or ""), "")
    critical = checks.is_critical(status)
    values = {
        "inspection_id": inspection_id,
        "equipment_type": kind,
        "checks_version": checks.CHECKS_VERSION,
        "results": results,
        "status": status,
        "issue": row.get("issue") or "",
        "corrective_action": row.get("corrective_action") or "",
        "engineer_remarks": row.get("engineer_remarks") or "",
        "evidence_photo_id": _mapped("site_inspection_photos", row.get("evidence_photo_id")),
        "review_status": review if (review and critical) else (C.AssessmentReview.PENDING if critical else C.AssessmentReview.NOT_REQUIRED),
        "resolved_at": timestamp(row.get("updated_at")) if review and critical else None,
        "resolution_note": f"Imported: legacy resolution {row.get('resolution_status')}." if review and critical else "",
    }
    target = run.find_target(EquipmentAssessment, row["id"], lambda: EquipmentAssessment.objects.filter(inspection_id=inspection_id, equipment_type=kind).first())
    upsert(run, EquipmentAssessment, row["id"], target=target, values=values, created_at=timestamp(row.get("created_at")), updated_at=timestamp(row.get("updated_at")))


def import_equipment(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import(
        ImportRun(SI, "site_inspection_equipment_assessments"), rows, _import_equipment, user=user, dry_run=dry_run, action=ACTION, object_type=OBJECT_TYPE, namespaces=(CACHE_NAMESPACE,)
    )


def _import_approval(run: ImportRun, row: dict) -> None:
    inspection_id = _mapped("site_inspections", row.get("inspection_id"))
    if inspection_id is None:
        run.violation(row["id"], "inspection_unmapped", "The inspection was not imported; approval skipped.")
        run.skipped += 1
        return
    status = str(row.get("status") or "").upper()
    if status == C.ApprovalStatus.PENDING or status not in C.ApprovalStatus.values:
        run.violation(row["id"], "approval_expired", f"Legacy {status or 'unknown'} approval (no customer link ever existed) imported as EXPIRED.")
        status = C.ApprovalStatus.EXPIRED
    if row.get("signature_url"):
        run.violation(row["id"], "signature_not_imported", "The legacy signature URL is not imported.")
    values = {
        "inspection_id": inspection_id,
        "number": int(row.get("version") or 1),
        "status": status,
        "location_snapshot": None,
        "customer_name": (row.get("customer_name") or "")[:255] or "—",
        "customer_phone_e164": try_normalise(row.get("customer_phone")) or "" if row.get("customer_phone") else "",
        "customer_comment": row.get("customer_comment") or "",
        "responded_at": timestamp(row.get("approved_at")) or timestamp(row.get("rejected_at")),
        "otp_verified_at": None,
    }
    target = run.find_target(LocationApproval, row["id"])
    upsert(run, LocationApproval, row["id"], target=target, values=values, created_at=timestamp(row.get("created_at")))


def import_approvals(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import(ImportRun(SI, "site_inspection_approvals"), rows, _import_approval, user=user, dry_run=dry_run, action=ACTION, object_type=OBJECT_TYPE, namespaces=(CACHE_NAMESPACE,))


def _import_observation(run: ImportRun, row: dict) -> None:
    inspection_id = _mapped("site_inspections", row.get("inspection_id"))
    if inspection_id is None or not str(row.get("note") or "").strip():
        run.violation(row["id"], "observation_unusable", "Inspection not imported or empty note; skipped.")
        run.skipped += 1
        return
    category = _enum(run, row["id"], "category", row.get("category"), C.ObservationCategory) or C.ObservationCategory.GENERAL
    stage = row.get("stage") if isinstance(row.get("stage"), int) and 1 <= row["stage"] <= 10 else None
    values = {"inspection_id": inspection_id, "note": row["note"], "category": category, "stage": stage}
    target = run.find_target(Observation, row["id"])
    observation = upsert(run, Observation, row["id"], target=target, values=values, created_at=timestamp(row.get("created_at")), updated_at=timestamp(row.get("updated_at")))
    photo_ids = [pk for pk in (_mapped("site_inspection_photos", legacy) for legacy in json.loads(row.get("photo_ids_json") or "[]") or []) if pk]
    observation.photos.set(Photo.objects.filter(pk__in=photo_ids, inspection_id=inspection_id))


def import_observations(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return run_import(ImportRun(SI, "site_inspection_observations"), rows, _import_observation, user=user, dry_run=dry_run, action=ACTION, object_type=OBJECT_TYPE, namespaces=(CACHE_NAMESPACE,))


def import_all(tables: dict[str, list[dict]], *, user=None, dry_run: bool = False) -> dict[str, dict]:
    """Every table in dependency order (each call is its own transaction; a dry run leaves nothing behind)."""
    report = {
        "engineers": import_engineers(tables.get("engineers", []), logins=tables.get("engineer_users", []), user=user, dry_run=dry_run),
        "customers": import_customers(tables.get("customers", []), user=user, dry_run=dry_run),
    }
    if dry_run:
        return report  # later tables need the mapped customers and inspections, which a dry run rolled back
    report["site_inspections"] = import_inspections(tables.get("site_inspections", []), user=user)
    report["site_inspection_photos"] = import_photos(tables.get("site_inspection_photos", []), user=user)
    link_layout(tables.get("site_inspections", []))
    report["site_inspection_annotations"] = import_annotations(tables.get("site_inspection_annotations", []), user=user)
    report["site_inspection_equipment_assessments"] = import_equipment(tables.get("site_inspection_equipment_assessments", []), user=user)
    report["site_inspection_approvals"] = import_approvals(tables.get("site_inspection_approvals", []), user=user)
    report["site_inspection_observations"] = import_observations(tables.get("site_inspection_observations", []), user=user)
    return report
