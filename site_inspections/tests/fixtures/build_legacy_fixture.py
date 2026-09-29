"""Build ``legacy_si.json``: rows of the Site Inspection V2 SQLite schema, as the legacy app writes them.

No production SQLite file exists in the reference estate, so the fixture is produced the way the legacy app would
produce it: the schema is created from the legacy source itself (the ``CREATE TABLE`` block of ``lib/db.ts`` and
every ``ensureColumn`` it applies), and the rows are written with the legacy encodings (spec §A.4): ``"No"`` strings
in INTEGER flag columns, ``"Yes"/"No"`` wheeling, ``PARTIAL`` underground cabling, 1/0 suitability, collapsed neutral
link/termination, vehicle-type labels, container-relative rectangles, base64 data-URL photos, keys-only equipment
results. Every personal value is invented (masked). Re-run (read-only on the legacy tree)::

    python site_inspections/tests/fixtures/build_legacy_fixture.py --si-root /path/to/site-inspection-v2
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sqlite3
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = Path("/tmp/claude-0/-home-user/2d0c9eb0-ce20-5c8e-9860-d18f6ad69685/scratchpad/utils/site-inspection-v2")
TABLES = (
    "customers",
    "engineers",
    "engineer_users",
    "site_inspections",
    "site_inspection_photos",
    "site_inspection_annotations",
    "site_inspection_equipment_assessments",
    "site_inspection_approvals",
    "site_inspection_observations",
)


def schema(db_ts: str) -> tuple[str, list[tuple[str, str, str]]]:
    block = re.search(r"db\.exec\(`(.*?)`\);", db_ts, re.S).group(1)
    columns = re.findall(r'ensureColumn\(\s*"(\w+)",\s*"(\w+)",\s*"([^"]+)"\s*\)', db_ts)
    columns += [("site_inspections", name, definition) for name, definition in re.findall(r'\["(\w+)", "([^"]+)"\]', db_ts)]
    return block, columns


def data_url(color) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 12), color).save(buffer, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


T0 = "2026-06-10T04:30:00.000Z"
T1 = "2026-06-12T09:15:00.000Z"
NO_FLAGS = {
    name: "No"
    for name in (
        "walkway_required",
        "ladder_required",
        "sliding_door_required",
        "underground_cabling",
        "extra_ac_cable",
        "extra_dc_cable",
        "new_neutral_link",
        "new_termination",
        "additional_earthing_required",
        "civil_work",
        "wheeling_required",
    )
}
PASS_HYBRID_INVERTER = {
    k: "PASS"
    for k in (
        "direct_sunlight",
        "rain_exposure",
        "water_leakage",
        "ventilation",
        "clearance",
        "heat_source",
        "dust_chemical_exposure",
        "physical_protection",
        "maintenance_access",
        "mounting_surface",
        "cable_entry",
        "height",
        "flooding_water_risk",
        "cable_route",
        "battery_distance",
        "battery_cable_length",
    )
}


def rows() -> dict[str, list[dict]]:
    customers = [
        {
            "id": "cust_a1",
            "customer_code": "PA-agr-1001",
            "name": "Anitha Varghese",
            "phone": "9847000101",
            "email": "anitha@example.com",
            "address": "House 12, Temple Road, Aluva 683101",
            "pincode": "683101",
            "location": "Aluva",
            "district": "Ernakulam",
            "created_at": T0,
            "updated_at": T0,
        },
        {
            "id": "cust_b2",
            "customer_code": "PRE-1718000000000",
            "name": "Biju Thomas",
            "phone": "+91 98470 00202",
            "email": None,
            "address": "Kaloor, Kochi",
            "pincode": "682017",
            "location": None,
            "district": "Ernakulam",
            "created_at": T0,
            "updated_at": T0,
        },
        {
            "id": "cust_c3",
            "customer_code": "PRE-1718000000001",
            "name": "Chitra Menon",
            "phone": "9847000303",
            "email": None,
            "address": "Palarivattom",
            "pincode": None,
            "location": None,
            "district": None,
            "created_at": T0,
            "updated_at": T0,
        },
        {
            "id": "cust_d4",
            "customer_code": "PRE-1718000000002",
            "name": "Dev (no phone)",
            "phone": "call office",
            "email": None,
            "address": None,
            "pincode": None,
            "location": None,
            "district": None,
            "created_at": T0,
            "updated_at": T0,
        },
    ]
    engineers = [
        {"id": "eng_1", "engineer_code": "ENG-001", "name": "Site Engineer 1", "phone": "", "status": "active", "created_at": T0, "updated_at": T0},
        {"id": "eng_2", "engineer_code": "ENG-002", "name": "Site Engineer 2", "phone": "9847000999", "status": "inactive", "created_at": T0, "updated_at": T0},
    ]
    engineer_users = [
        {"id": "eu_1", "engineer_id": "eng_1", "username": "engineer", "password_hash": "00:00", "active": 1, "created_at": T0, "updated_at": T0},
        {"id": "eu_2", "engineer_id": "eng_2", "username": "engineer2", "password_hash": "00:00", "active": 0, "created_at": T0, "updated_at": T0},
    ]
    snapshot_a = {
        "inspectionOrigin": "PA_INSPECTION",
        "systemType": "HYBRID",
        "purchaseAgreement": {"id": "agr-1001", "version": 2},
        "customer": {
            "id": "cust_a1",
            "name": "Anitha Varghese",
            "phone": "9847000101",
            "email": "anitha@example.com",
            "address": "House 12, Temple Road, Aluva 683101",
            "pincode": "683101",
            "location": None,
            "district": None,
        },
        "site": {"address": "House 12, Temple Road, Aluva 683101", "pincode": "683101", "location": None, "district": None},
        "system": {
            "type": "HYBRID",
            "capacityKw": 5,
            "phase": "Three Phase",
            "panel": {"brand": "Waaree", "model": "Waaree - 545W", "capacityW": 545, "quantity": None},
            "inverter": {"brand": "Deye", "type": "Hybrid"},
            "battery": {"option": "5 kWh"},
            "structure": {"material": "GI", "quotedType": "GI"},
        },
        "projectId": None,
        "kseb": {"consumerNumber": "1156780001234", "registeredPhone": "9847000101", "wheelingRequired": False},
    }
    inspections = [
        {
            **NO_FLAGS,
            "id": "si_a",
            "site_visit_no": "SV-20260610-0001",
            "site_visit_date": "2026-06-10",
            "engineer_id": "eng_1",
            "status": "INSTALLATION_READY",
            "customer_id": "cust_a1",
            "purchase_agreement_id": "agr-1001",
            "purchase_agreement_version": 2,
            "inspection_snapshot_json": json.dumps(snapshot_a),
            "inspection_origin": "PA_INSPECTION",
            "system_type": "HYBRID",
            "complexity_status": "ROUTINE",
            "address": "House 12, Temple Road, Aluva 683101",
            "pincode": "683101",
            "district": "Ernakulam",
            "latitude": 10.1076,
            "longitude": 76.3516,
            "location_accuracy": 8.5,
            "location_captured_at": T0,
            "google_map_link": "https://www.google.com/maps?q=10.1076,76.3516",
            "road_access": "GOOD",
            "vehicle_type": "Truck",
            "roof_type": "RCC_FLAT",
            "roof_condition": "GOOD",
            "roof_strength": "GOOD",
            "roof_accessibility": "EASY",
            "roof_length": 12.5,
            "roof_width": 8,
            "usable_area": 70,
            "direction_facing": "S",
            "morning_shading": "LOW",
            "afternoon_shading": "NONE",
            "shading_percentage": 5,
            "generation_impact": "LOW",
            "consumer_number": "1156780001234",
            "registered_phone": "9847000101",
            "phase": "THREE_PHASE",
            "connected_load": "7.5",
            "sanctioned_load": "8",
            "tariff": "LT_I",
            "neutral_link": "AVAILABLE",
            "termination_point": "AVAILABLE",
            "distribution_board": "AVAILABLE",
            "earthing": "NEEDS_MODIFICATION",
            "ac_cable_measurement": 15,
            "dc_cable_measurement": 25,
            "la_cable_measurement": 12,
            "battery_cable_length": 3,
            "battery_cable_route": "Along the wall",
            "underground_cabling": "PARTIAL",
            "ug_cable_length": 6,
            "extra_ac_cable": "NO",
            "extra_dc_cable": "YES",
            "extra_dc_cable_length": 10,
            "additional_earthing_required": "YES",
            "site_suitable_for_solar": 1,
            "final_recommendation": "SUITABLE",
            "suitability_percentage": 92,
            "engineer_remarks": "Good site",
            "has_location_restrictions": 0,
            "quoted_solar_size": 5,
            "quoted_panel_brand": "Waaree",
            "quoted_panel_capacity": "545 Wp",
            "quoted_inverter_brand": "Deye",
            "quoted_inverter_type": "Hybrid",
            "quoted_battery_option": "5 kWh",
            "quoted_structure_material": "GI",
            "quoted_structure_type": "GI",
            "quoted_original_price": 410000,
            "quoted_discount": 10000,
            "quoted_final_price": 400000,
            "site_structure_type": "SUFFICIENT",
            "panel_photo_id": "photo_a1",
            "equipment_photo_id": "photo_a2",
            "panel_width": 6,
            "panel_height": 4,
            "panel_area": 24,
            "equipment_width": 2,
            "equipment_height": 1.5,
            "equipment_area": 3,
            "additional_work_status": "APPROVED",
            "installation_readiness": "INSTALLATION_READY",
            "approved_at": T1,
            "created_by": "purchase-agreement-integration",
            "created_at": T0,
            "updated_by": "system",
            "updated_at": T1,
        },
        {
            **NO_FLAGS,
            "id": "si_b",
            "site_visit_no": "SV-20260611-0001",
            "site_visit_date": "2026-06-11",
            "engineer_id": "eng_1",
            "status": "IN_PROGRESS",
            "customer_id": "cust_b2",
            "purchase_agreement_id": None,
            "purchase_agreement_version": None,
            "inspection_snapshot_json": None,
            "inspection_origin": "PRE_SALE",
            "system_type": "UNDECIDED",
            "complexity_status": "ENGINEERING_REVIEW_REQUIRED",
            "complexity_reason": "Engineer marked site as complex",
            "address": "Kaloor, Kochi",
            "pincode": "682017",
            "vehicle_type": "Pickup / Van",
            "roof_type": "TILE",
            "roof_condition": "REVIEW_REQUIRED",
            "wheeling_required": "Yes",
            "consumer_number": None,
            "registered_phone": "12345",
            "phase": "Single phase",
            "neutral_link": "NEEDS_MODIFICATION",
            "termination_point": "NEEDS_MODIFICATION",
            "site_suitable_for_solar": 0,
            "final_recommendation": "CONDITIONAL",
            "has_location_restrictions": "1",
            "customer_location_remarks": None,
            "site_structure_type": "REQUIRES_ENGINEERING_REVIEW",
            "additional_work_status": "IDENTIFIED",
            "installation_readiness": "NOT_READY",
            "created_by": "system",
            "created_at": T0,
            "updated_by": "eng_1",
            "updated_at": T1,
        },
        {
            **NO_FLAGS,
            "id": "si_c",
            "site_visit_no": "SV-20260612-0001",
            "site_visit_date": "2026-06-12",
            "engineer_id": "eng_2",
            "status": "ON_HOLD",
            "customer_id": "cust_c3",
            "purchase_agreement_id": None,
            "purchase_agreement_version": None,
            "inspection_snapshot_json": None,
            "inspection_origin": "PRE_SALE",
            "system_type": "ON_GRID",
            "complexity_status": "NOT_ASSESSED",
            "address": "Palarivattom",
            "roof_type": "ASBESTOS",
            "shading_percentage": 140,
            "walkway_required": "YES",
            "walkway_length": 7.5,
            "ladder_required": 1,
            "ladder_length": 4,
            "other_additional_work": "Shift the water tank",
            "additional_work_status": "NONE",
            "installation_readiness": "NOT_READY",
            "created_by": "system",
            "created_at": T1,
            "updated_by": "system",
            "updated_at": T1,
        },
    ]
    photos = [
        {
            "id": "photo_a1",
            "inspection_id": "si_a",
            "photo_type": "PANEL_AREA",
            "file_url": data_url((200, 120, 40)),
            "original_filename": "panel.jpg",
            "mime_type": "image/jpeg",
            "width": 16,
            "height": 12,
            "upload_status": "PENDING_UPLOAD",
            "captured_at": T0,
            "created_by": "eng_1",
            "created_at": T0,
        },
        {
            "id": "photo_a2",
            "inspection_id": "si_a",
            "photo_type": "EQUIPMENT_AREA",
            "file_url": data_url((40, 120, 200)),
            "original_filename": "inverter.jpg",
            "mime_type": "image/jpeg",
            "width": 16,
            "height": 12,
            "upload_status": "PENDING_UPLOAD",
            "captured_at": T0,
            "created_by": "eng_1",
            "created_at": T0,
        },
        {
            "id": "photo_a3",
            "inspection_id": "si_a",
            "photo_type": "CABLE_ROUTING",
            "file_url": data_url((90, 90, 90)),
            "original_filename": "cable.jpg",
            "mime_type": "image/jpeg",
            "width": 16,
            "height": 12,
            "upload_status": "PENDING_UPLOAD",
            "captured_at": None,
            "created_by": "eng_1",
            "created_at": T0,
        },
        {
            "id": "photo_b1",
            "inspection_id": "si_b",
            "photo_type": "SITE_ACCESS",
            "file_url": "data:image/jpeg;base64,not-an-image",
            "original_filename": "broken.jpg",
            "mime_type": "image/jpeg",
            "width": None,
            "height": None,
            "upload_status": "PENDING_UPLOAD",
            "captured_at": None,
            "created_by": "eng_1",
            "created_at": T1,
        },
    ]
    annotations = [
        {
            "id": "ann_a1",
            "inspection_id": "si_a",
            "photo_id": "photo_a1",
            "annotation_type": "PANEL_AREA",
            "label": "Proposed panel area",
            "geometry_json": json.dumps({"x": 0.12, "y": 0.18, "width": 0.76, "height": 0.56}),
            "metadata_json": json.dumps({"widthM": 6, "heightM": 4, "areaM2": 24}),
            "created_by": "eng_1",
            "created_at": T0,
            "updated_at": T0,
        },
        {
            "id": "ann_a2",
            "inspection_id": "si_a",
            "photo_id": "photo_a2",
            "annotation_type": "EQUIPMENT_AREA",
            "label": "Proposed equipment area",
            "geometry_json": json.dumps({"x": 0.18, "y": 0.18, "width": 0.64, "height": 0.52}),
            "metadata_json": json.dumps({"widthM": 2, "heightM": 1.5, "areaM2": 3}),
            "created_by": "eng_1",
            "created_at": T0,
            "updated_at": T0,
        },
    ]
    equipment = [
        {
            "id": "EQA-1718000000000-a1b2c3",
            "inspection_id": "si_a",
            "equipment_type": "HYBRID_INVERTER",
            "status": "PASS",
            "results_json": json.dumps(PASS_HYBRID_INVERTER),
            "issue": "",
            "engineer_remarks": "",
            "corrective_action": "",
            "evidence_photo_id": None,
            "engineering_review_status": "NOT_REQUIRED",
            "resolution_status": "OPEN",
            "created_at": T0,
            "updated_at": T0,
        },
        {
            "id": "EQA-1718000000001-d4e5f6",
            "inspection_id": "si_a",
            "equipment_type": "HYBRID_BATTERY",
            "status": "FAIL",
            "results_json": json.dumps({"safe_location": "PASS", "ventilation": "FAIL"}),
            "issue": "Closed store room",
            "engineer_remarks": "",
            "corrective_action": "Add vent",
            "evidence_photo_id": "photo_a3",
            "engineering_review_status": "NOT_REQUIRED",
            "resolution_status": "WAIVED_APPROVED",
            "created_at": T0,
            "updated_at": T1,
        },
        {
            "id": "EQA-1718000000002-g7h8i9",
            "inspection_id": "si_c",
            "equipment_type": "ON_GRID_INVERTER",
            "status": "PASS",
            "results_json": json.dumps({"direct_sunlight": "PASS"}),
            "issue": "",
            "engineer_remarks": "",
            "corrective_action": "",
            "evidence_photo_id": None,
            "engineering_review_status": "NOT_REQUIRED",
            "resolution_status": "OPEN",
            "created_at": T1,
            "updated_at": T1,
        },
    ]
    approvals = [
        {
            "id": "approval_a1",
            "inspection_id": "si_a",
            "approval_type": "INSTALLATION_LOCATION",
            "version": 1,
            "status": "APPROVED",
            "proposed_location_id": None,
            "customer_name": "Anitha Varghese",
            "customer_phone": "9847000101",
            "customer_comment": "Fine",
            "signature_url": None,
            "approved_at": T1,
            "rejected_at": None,
            "created_at": T0,
        },
        {
            "id": "approval_b1",
            "inspection_id": "si_b",
            "approval_type": "INSTALLATION_LOCATION",
            "version": 1,
            "status": "PENDING",
            "proposed_location_id": None,
            "customer_name": "Biju Thomas",
            "customer_phone": "12345",
            "customer_comment": None,
            "signature_url": None,
            "approved_at": None,
            "rejected_at": None,
            "created_at": T1,
        },
    ]
    observations = [
        {
            "id": "obs_a1",
            "inspection_id": "si_a",
            "category": "General",
            "note": "Water tank to be shifted by customer",
            "photo_ids_json": json.dumps(["photo_a3"]),
            "stage": 8,
            "created_by": "eng_1",
            "created_at": T0,
            "updated_at": T0,
        },
        {"id": "obs_c1", "inspection_id": "si_c", "category": "Earthing", "note": "Earth pit missing", "photo_ids_json": None, "stage": 5, "created_by": "eng_2", "created_at": T1, "updated_at": T1},
    ]
    return {
        "customers": customers,
        "engineers": engineers,
        "engineer_users": engineer_users,
        "site_inspections": inspections,
        "site_inspection_photos": photos,
        "site_inspection_annotations": annotations,
        "site_inspection_equipment_assessments": equipment,
        "site_inspection_approvals": approvals,
        "site_inspection_observations": observations,
    }


def build(si_root: Path) -> dict[str, list[dict]]:
    block, columns = schema((si_root / "lib" / "db.ts").read_text())
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(block)
    for table, name, definition in columns:
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    for table, items in rows().items():
        for item in items:
            names = ", ".join(item)
            db.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' for _ in item)})", list(item.values()))
    # Export exactly what the SQLite tables hold (every column, the legacy types preserved).
    return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in TABLES}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--si-root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    (HERE / "legacy_si.json").write_text(json.dumps(build(args.si_root), indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
