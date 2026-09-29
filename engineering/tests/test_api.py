"""``/api/v1/engineering/rule-sets/``, ``runs/``, ``findings/<uid>/acknowledge/``."""

from __future__ import annotations

import pytest

from core.models import OutboxEvent
from engineering.models import Acknowledgement, Finding, RuleSet, Run
from engineering.services import rule_sets, runs
from engines.engineering_checker import DEFAULT_RULE_SET, check_project_bom

pytestmark = pytest.mark.django_db
BASE = "/api/v1/engineering/"


@pytest.fixture
def admin(auth_client, make_user):
    return auth_client(make_user(grants={"engineering": "*"}))


@pytest.fixture
def viewer(auth_client, make_user):
    return auth_client(make_user(grants={"engineering": ["view"]}))


@pytest.fixture
def run(db):
    result = check_project_bom(bom={"phase": "1P", "architecture": "DEYE", "sysType": "ongrid"}, lines=[{"role": "PANEL", "componentId": "p1", "quantity": 6}], template_scope=True)
    return runs.record_run(
        rule_set=rule_sets.active_rule_set(),
        subject_type="PROJECT_BOM",
        subject_uid="11111111-1111-1111-1111-111111111111",
        subject_label="demo",
        checks=[("", result)],
        user=None,
    )


class TestRuleSets:
    def test_seeded_and_listed(self, viewer):
        body = viewer.get(f"{BASE}rule-sets/").json()
        versions = {row["rules_version"]: row for row in body["results"]}
        assert versions["phase1e.1"]["active"] and versions["phase1e.1"]["rule_count"] == 35
        assert versions["phase1e.eng"]["engine"] == "engineeringValidation"
        detail = viewer.get(f"{BASE}rule-sets/{versions['phase1e.1']['uid']}/").json()
        assert detail["rules"]["version"] == "phase1e.1"

    def test_access(self, api_client, viewer, auth_client, make_user):
        assert api_client.get(f"{BASE}rule-sets/").status_code == 401
        assert auth_client(make_user(grants={"packs": ["view"]})).get(f"{BASE}rule-sets/").status_code == 403
        uid = RuleSet.objects.first().uid
        assert viewer.post(f"{BASE}rule-sets/{uid}/activate/", {}, format="json").status_code == 403

    def test_activate_exactly_one(self, admin):
        seeded = RuleSet.objects.get(rules_version="phase1e.1")
        stricter = RuleSet.objects.create(rules_version="phase1e.2", engine="engineeringChecker", rules=DEFAULT_RULE_SET.with_changes(version="phase1e.2", severities={"PBC-D-003": "BLOCK"}).as_json())
        stale = admin.post(f"{BASE}rule-sets/{stricter.uid}/activate/", {"expected_version": 5}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        response = admin.post(f"{BASE}rule-sets/{stricter.uid}/activate/", {"note": "stricter"}, format="json")
        assert response.status_code == 200 and response.json()["active"] is True
        seeded.refresh_from_db()
        assert not seeded.active and RuleSet.objects.filter(engine="engineeringChecker", active=True).count() == 1
        assert OutboxEvent.objects.filter(event_type="engineering.rule_set_activated").exists()
        again = admin.post(f"{BASE}rule-sets/{stricter.uid}/activate/", {}, format="json")
        assert again.status_code == 409 and again.json()["code"] == "rule_set_already_active"
        assert rule_sets.engine_rule_set(stricter).rule("PBC-D-003").severity.value == "BLOCK"

    def test_invalid_document_is_refused(self, admin):
        broken = RuleSet.objects.create(
            rules_version="broken", engine="engineeringChecker", rules={"engine": "engineeringChecker", "version": "broken", "rules": [{"code": "PBC-A-001", "severity": "LOUD"}]}
        )
        response = admin.post(f"{BASE}rule-sets/{broken.uid}/activate/", {}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "rule_set_invalid"


class TestRuns:
    def test_list_detail_filters(self, viewer, run, django_assert_max_num_queries):
        with django_assert_max_num_queries(10):
            body = viewer.get(f"{BASE}runs/").json()
        assert body["count"] == 1 and body["results"][0]["result"] == "FAIL"
        assert viewer.get(f"{BASE}runs/", {"result": "PASS"}).json()["count"] == 0
        assert viewer.get(f"{BASE}runs/", {"subject_uid": "11111111-1111-1111-1111-111111111111"}).json()["count"] == 1
        detail = viewer.get(f"{BASE}runs/{run.uid}/").json()
        assert detail["findings"] and all(f["acknowledgement"] is None for f in detail["findings"])
        assert viewer.get(f"{BASE}runs/", {"result": "MAYBE"}).status_code == 400

    def test_acknowledge_and_carry_over(self, admin, viewer, run):
        warn = Finding.objects.filter(run=run, severity="WARN").first() or Finding.objects.filter(run=run).first()
        block = Finding.objects.filter(run=run, severity="BLOCK").first()
        assert viewer.post(f"{BASE}findings/{warn.uid}/acknowledge/", {"reason": "ok"}, format="json").status_code == 403
        blank = admin.post(f"{BASE}findings/{block.uid}/acknowledge/", {"reason": " "}, format="json")
        assert blank.status_code == 400 and blank.json()["code"] == "validation_error"
        stale = admin.post(f"{BASE}findings/{block.uid}/acknowledge/", {"reason": "waived", "expected_version": 9}, format="json")
        assert stale.json()["code"] == "stale_version"
        response = admin.post(f"{BASE}findings/{block.uid}/acknowledge/", {"reason": "waived for the pilot"}, format="json")
        assert response.status_code == 201 and response.json()["reason"] == "waived for the pilot"
        again = admin.post(f"{BASE}findings/{block.uid}/acknowledge/", {"reason": "x"}, format="json")
        assert again.status_code == 409 and again.json()["code"] == "finding_already_acknowledged"
        assert Acknowledgement.objects.count() == 1
        assert block.identity in runs.acknowledged_identities("PROJECT_BOM", run.subject_uid)
        assert admin.post(f"{BASE}findings/00000000-0000-0000-0000-000000000000/acknowledge/", {"reason": "x"}, format="json").status_code == 404
        assert OutboxEvent.objects.filter(event_type="engineering.finding_acknowledged").exists()

    def test_access(self, api_client, auth_client, make_user, run):
        finding = Finding.objects.filter(run=run).first()
        assert api_client.get(f"{BASE}runs/").status_code == 401
        assert api_client.get(f"{BASE}runs/{run.uid}/").status_code == 401
        assert api_client.post(f"{BASE}findings/{finding.uid}/acknowledge/", {"reason": "x"}, format="json").status_code == 401
        outsider = auth_client(make_user(grants={"packs": ["view"]}))
        assert outsider.get(f"{BASE}runs/").status_code == 403
        assert outsider.get(f"{BASE}runs/{run.uid}/").status_code == 403
        assert outsider.post(f"{BASE}findings/{finding.uid}/acknowledge/", {"reason": "x"}, format="json").status_code == 403
        assert not Acknowledgement.objects.exists()

    def test_no_active_rule_set(self):
        RuleSet.objects.update(active=False)
        with pytest.raises(Exception) as caught:
            rule_sets.active_rule_set()
        assert caught.value.code == "no_active_rule_set"
        assert Run.objects.count() == 0
