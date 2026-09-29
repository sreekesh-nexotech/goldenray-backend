"""Flarize ``workspace-state.json`` / ``bom-state.json`` import (fixtures exported read-only from the Flarize data
directory, customer names, phones and addresses masked)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from accounts.tests.factories import UserFactory
from audit.models import AuditLog
from catalog.tests.factories import ComponentFactory
from core.models import LegacyMap
from customers.models import Customer
from customers.tests.factories import CustomerFactory
from projects.models import Project
from projects.services import projects
from projects.services.legacy_import import import_flarize_workspace_projects, report_flarize_bom_state

pytestmark = pytest.mark.django_db
FIXTURES = Path(__file__).parent / "fixtures"
ALT = "PRJ-ALT-ongrid_3_base_20260913132522769j9e1"
BY_PHONE = "PRJ-ongrid_3_value_20260830170726"


def _rows():
    return list(json.loads((FIXTURES / "flarize_workspace_state.json").read_text())["projects"].values())


@pytest.fixture
def sources(db):
    admin = UserFactory()
    LegacyMap.objects.create(source_system="FLARIZE", source_table="users", source_id="admin-001", target_table="accounts_user", target_id=admin.pk)
    imported = CustomerFactory(code="CUST-TMP")
    LegacyMap.objects.create(source_system="FLARIZE", source_table="customers", source_id="CUST-MTBP1EAI-582059", target_table="customers_customer", target_id=imported.pk)
    by_phone = CustomerFactory(phone_e164="+919847000004")
    panel = ComponentFactory(sku="a1", name="ACDB 1P")
    return {"admin": admin, "imported": imported, "by_phone": by_phone, "a1": panel}


def test_import_locked_projects(sources):
    result = import_flarize_workspace_projects(_rows())
    assert (result["created"], result["updated"], result["skipped"]) == (2, 0, 2)
    codes = Counter(v["code"] for v in result["violations"])
    assert codes["open_workspace_not_migrated"] == 1 and codes["customer_not_found"] == 1
    assert codes["unmapped_user"] >= 1 and codes["unknown_component"] >= 1
    alt = Project.objects.get(pk=LegacyMap.objects.get(source_table="workspace_projects", source_id=ALT).target_id)
    assert alt.customer == sources["imported"] and alt.status == "IN_PROGRESS" and alt.system_type == "ON_GRID" and alt.tier == "BASE"
    assert str(alt.size_kw) == "3.00" and alt.phase == "1P" and alt.title and alt.number.startswith("PROJ-")
    assert alt.bom_locked_by == sources["admin"] and alt.created_by == sources["admin"] and alt.bom_locked_at.isoformat().startswith("2026-09-13")
    snapshot = alt.bom_lock
    assert snapshot["legacy"] is True and snapshot["engineering"]["result"] == "WARN" and snapshot["engineering"]["rules_version"] == "phase1e.1"
    assert len(snapshot["lines"]) == 11 and len(snapshot["acknowledgements"]) == 2 and snapshot["locked_by"] == str(sources["admin"].uid)
    acdb = next(line for line in snapshot["lines"] if line["sku"] == "a1")
    assert acdb["component_uid"] == str(sources["a1"].uid) and acdb["role"] == "ACDB"
    assert alt.cost_inputs == {"distance_km": 100, "vehicle_type": "ACE", "installation_type": "FLAT", "size_key": "3"}
    by_phone = Project.objects.get(pk=LegacyMap.objects.get(source_table="workspace_projects", source_id=BY_PHONE).target_id)
    assert by_phone.customer == sources["by_phone"] and by_phone.bom_locked_by is None
    assert AuditLog.objects.filter(action="projects.legacy_import").exists()


def test_rerun_is_idempotent_and_never_rewinds(sources):
    import_flarize_workspace_projects(_rows())
    again = import_flarize_workspace_projects(_rows())
    assert (again["created"], again["updated"]) == (0, 0) and Project.objects.count() == 2
    alt = Project.objects.get(pk=LegacyMap.objects.get(source_table="workspace_projects", source_id=ALT).target_id)
    projects.commission(alt, user=None)
    rows = _rows()
    rows[0]["costInputs"]["distanceKm"] = 120
    third = import_flarize_workspace_projects(rows)
    alt.refresh_from_db()
    assert third["updated"] == 1 and alt.status == "COMMISSIONED" and alt.cost_inputs["distance_km"] == 120


def test_dry_run_and_bad_rows(sources):
    rows = _rows()
    rows[1] = {**rows[1], "sysType": "wind", "phase": "2P", "sizeKw": 0}
    rows.append({"projectId": ""})
    result = import_flarize_workspace_projects(rows, dry_run=True)
    codes = Counter(v["code"] for v in result["violations"])
    assert codes["unknown_system_type"] == 1 and codes["unknown_phase"] == 1 and codes["invalid_size"] == 1 and codes["incomplete_row"] == 1
    assert result["created"] == 2 and not Project.objects.exists() and Customer.objects.count() == 2


def test_bom_state_is_reported_not_migrated():
    state = json.loads((FIXTURES / "flarize_bom_state.json").read_text())
    report = report_flarize_bom_state(state)
    assert report["created"] == 0 and report["skipped"] == 1 and report["violations"][0]["code"] == "ui_state_not_migrated"
    assert report_flarize_bom_state(None)["violations"] == []


def test_installation_type_upper_cased_and_merged_customer_followed(sources):
    """Flarize kept some ``installationType`` values in lower case (6 of the 186 locked projects: ``flat``); the platform
    stores the ``InstallationType`` value the API validates. A customer merged after the first import is followed to the
    survivor on a re-run instead of losing the mapping."""
    rows = _rows()
    rows[0]["costInputs"]["installationType"] = "flat"
    import_flarize_workspace_projects(rows)
    alt = Project.objects.get(pk=LegacyMap.objects.get(source_table="workspace_projects", source_id=ALT).target_id)
    assert alt.cost_inputs["installation_type"] == "FLAT"
    survivor = CustomerFactory()
    Customer.objects.filter(pk=sources["imported"].pk).update(merged_into=survivor, deleted_at=survivor.created_at)
    again = import_flarize_workspace_projects(rows)
    alt.refresh_from_db()
    assert alt.customer == survivor and "customer_not_found" not in {v["code"] for v in again["violations"] if v["source_id"] == ALT}
