"""``/api/v1/packs/config-versions/`` — reads, drafts, config edits, pins, checker, submit/approve/reject."""

from __future__ import annotations

import pytest

from audit.models import AuditLog
from core.models import OutboxEvent
from engineering.models import Run
from packs.models import ConfigLine, ConfigPack, ConfigPin, ConfigStatus, ConfigVersion
from packs.tests import factories

pytestmark = pytest.mark.django_db
BASE = "/api/v1/packs/config-versions/"


def _draft(admin, world):
    response = admin.post(BASE, {}, format="json")
    assert response.status_code == 201, response.json()
    return response.json()


class TestAccess:
    def test_anonymous_is_401(self, api_client, world):
        assert api_client.get(BASE).status_code == 401
        assert api_client.post(BASE, {}, format="json").status_code == 401

    def test_without_permission_is_403(self, outsider, world):
        assert outsider.get(BASE).status_code == 403
        assert outsider.get(f"{BASE}{world['version'].uid}/").status_code == 403

    def test_viewer_reads_but_cannot_write(self, viewer, world):
        assert viewer.get(BASE).status_code == 200
        assert viewer.post(BASE, {}, format="json").status_code == 403
        assert viewer.post(f"{BASE}{world['version'].uid}/submit/", {}, format="json").status_code == 403
        assert viewer.post(f"{BASE}{world['version'].uid}/run-checker/", {}, format="json").status_code == 403
        assert viewer.patch(f"{BASE}{world['version'].uid}/", {"sections": {"marketRates": {}}}, format="json").status_code == 403
        assert viewer.put(f"{BASE}{world['version'].uid}/packs/ongrid-value-3/", {"pins": {}}, format="json").status_code == 403
        assert viewer.post(f"{BASE}{world['version'].uid}/approve/", {"direct": True}, format="json").status_code == 403
        assert viewer.post(f"{BASE}{world['version'].uid}/reject/", {"reason": "x"}, format="json").status_code == 403

    def test_editor_cannot_approve_or_reject(self, editor, world):
        draft = editor.post(BASE, {}, format="json").json()
        assert editor.post(f"{BASE}{draft['uid']}/approve/", {"direct": True}, format="json").status_code == 403
        assert editor.post(f"{BASE}{draft['uid']}/reject/", {"reason": "x"}, format="json").status_code == 403

    def test_record_scope_all_sees_every_version(self, auth_client, make_user, world):
        client = auth_client(make_user(grants={"packs": ["view"]}, scopes={"packs": "all"}))
        assert client.get(BASE).json()["count"] == 1


class TestRead:
    def test_list_and_detail(self, viewer, world):
        body = viewer.get(BASE).json()
        assert body["count"] == 1 and body["results"][0]["number"] == 1 and body["results"][0]["status"] == "APPROVED"
        assert "config" not in body["results"][0]
        detail = viewer.get(f"{BASE}{world['version'].uid}/").json()
        assert detail["config"]["marketRates"] == {"ongrid_value": {"3": 229000}} and detail["has_config"] is True

    def test_filter_by_status(self, viewer, world, admin):
        _draft(admin, world)
        assert viewer.get(BASE, {"status": "DRAFT"}).json()["count"] == 1
        assert viewer.get(BASE, {"status": "PUBLISHED"}).json()["count"] == 0

    def test_list_query_count(self, viewer, world, django_assert_max_num_queries):
        for number in range(2, 6):
            ConfigVersion.objects.create(number=number, status=ConfigStatus.SUPERSEDED)
        with django_assert_max_num_queries(12):
            assert viewer.get(BASE).status_code == 200

    def test_packs_of_a_version(self, viewer, world, django_assert_max_num_queries):
        with django_assert_max_num_queries(16):
            body = viewer.get(f"{BASE}{world['version'].uid}/packs/").json()
        keys = [row["key"] for row in body["results"]]
        assert keys == ["ongrid-value-3", "ongrid-value-3-up5", "ongrid-value-5"]
        pack = body["results"][0]
        assert pack["panel"]["sku"] == "pnl1" and pack["inverter"]["sku"] == "inv1"
        assert [(line["slot_key"], line["source"]) for line in pack["lines"]] == [("panel", "SLOT"), ("inverter", "SLOT"), ("isolator", "SLOT"), ("fixed", "FIXED"), ("structure", "STRUCTURE")]
        fr = body["results"][1]
        assert fr["is_future_ready"] is True and fr["pair_of_key"] == "ongrid-value-3" and fr["future_size_key"] == "5"
        assert viewer.get(f"{BASE}{world['version'].uid}/packs/", {"tier": "BASE"}).json()["count"] == 0
        assert viewer.get(f"{BASE}{world['version'].uid}/packs/ongrid-value-3/").json()["key"] == "ongrid-value-3"
        assert viewer.get(f"{BASE}{world['version'].uid}/packs/nope/").json()["code"] == "pack_not_found"


class TestDraft:
    def test_create_copies_the_approved_version(self, admin, world):
        draft = _draft(admin, world)
        assert draft["number"] == 2 and draft["status"] == "DRAFT" and draft["based_on_number"] == 1
        assert draft["config"] == world["version"].config
        assert ConfigPack.objects.filter(config_version__uid=draft["uid"]).count() == 3
        assert AuditLog.objects.filter(action="packs.config_draft_created").exists()
        assert OutboxEvent.objects.filter(event_type="packs.config_draft_created").exists()

    def test_one_open_draft(self, admin, world):
        _draft(admin, world)
        response = admin.post(BASE, {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "draft_exists"

    def test_based_on_unknown_is_400(self, admin, world):
        response = admin.post(BASE, {"based_on_uid": "00000000-0000-0000-0000-000000000000"}, format="json")
        assert response.status_code == 400 and "based_on_uid" in response.json()["errors"]

    def test_based_on_history_without_config_is_409(self, admin, world):
        old = ConfigVersion.objects.create(number=7, status=ConfigStatus.SUPERSEDED)
        response = admin.post(BASE, {"based_on_uid": str(old.uid)}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "based_on_has_no_config"

    def test_first_version_needs_a_config(self, admin, db):
        response = admin.post(BASE, {}, format="json")
        assert response.status_code == 400 and "config" in response.json()["errors"]
        bad = admin.post(BASE, {"config": {"marketRate": {}}}, format="json")
        assert bad.status_code == 400 and bad.json()["code"] == "invalid_config"
        ok = admin.post(BASE, {"config": factories.config()}, format="json")
        assert ok.status_code == 201 and ok.json()["number"] == 1


class TestEdit:
    def test_patch_sections_and_change_log(self, admin, world):
        draft = _draft(admin, world)
        rates = {"ongrid_value": {"3": 239000}}
        response = admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"marketRates": rates}, "note": "new rate", "expected_version": draft["version"]}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["config"]["marketRates"] == rates and body["version"] == draft["version"] + 1
        assert body["change_log"][-1]["section"] == "marketRates" and body["change_log"][-1]["note"] == "new rate"

    def test_stale_version(self, admin, world):
        draft = _draft(admin, world)
        response = admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"marketRates": {}}, "expected_version": 99}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    def test_validation(self, admin, world):
        draft = _draft(admin, world)
        empty = admin.patch(f"{BASE}{draft['uid']}/", {}, format="json")
        assert empty.status_code == 400 and empty.json()["code"] == "validation_error"
        unknown = admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"marketRate": {}}}, format="json")
        assert unknown.status_code == 400 and unknown.json()["code"] == "invalid_config"
        invalid = admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"gst": {"ratePct": 150}}}, format="json")
        assert invalid.status_code == 400 and invalid.json()["code"] == "invalid_config"

    def test_only_open_drafts_are_edited(self, admin, world):
        response = admin.patch(f"{BASE}{world['version'].uid}/", {"sections": {"marketRates": {}}}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "version_not_editable"

    def test_editing_a_submitted_draft_withdraws_it(self, admin, world):
        draft = _draft(admin, world)
        admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"marketRates": {"ongrid_value": {"3": 1}}}}, format="json")
        assert admin.post(f"{BASE}{draft['uid']}/submit/", {}, format="json").json()["status"] == "SUBMITTED"
        body = admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"marketRates": {"ongrid_value": {"3": 2}}}}, format="json").json()
        assert body["status"] == "DRAFT" and body["submitted_at"] is None


class TestPins:
    def test_pin_changes_the_resolved_bom(self, admin, world):
        draft = _draft(admin, world)
        url = f"{BASE}{draft['uid']}/packs/ongrid-value-5/"
        response = admin.put(url, {"pins": {"panel": str(world["pnl2"].uid)}}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["panel"]["sku"] == "pnl2" and body["pins"][0]["slot_key"] == "panel"
        assert body["lines"][0]["source"] == "MANUAL"
        cleared = admin.put(url, {"pins": {}}, format="json").json()
        assert cleared["pins"] == [] and cleared["panel"]["sku"] == "pnl1"
        assert ConfigPin.all_objects.filter(pack__key="ongrid-value-5").count() == 1

    def test_pin_validation(self, admin, world):
        draft = _draft(admin, world)
        url = f"{BASE}{draft['uid']}/packs/ongrid-value-3/"
        response = admin.put(url, {"pins": {"panel": str(world["inv1"].uid), "battery": str(world["pnl1"].uid), "inverter": "00000000-0000-0000-0000-000000000000"}}, format="json")
        body = response.json()
        assert response.status_code == 400 and set(body["errors"]) == {"pins.panel", "pins.battery", "pins.inverter"}
        world["pnl2"].status = "RETIRED"
        world["pnl2"].save()
        retired = admin.put(url, {"pins": {"panel": str(world["pnl2"].uid)}}, format="json")
        assert retired.status_code == 400 and retired.json()["code"] == "validation_error"
        assert admin.put(f"{BASE}{draft['uid']}/packs/unknown/", {"pins": {}}, format="json").status_code == 404
        assert admin.put(url, {"pins": {}, "expected_version": 99}, format="json").json()["code"] == "stale_version"
        assert admin.put(f"{BASE}{world['version'].uid}/packs/ongrid-value-3/", {"pins": {}}, format="json").json()["code"] == "version_not_editable"


class TestLifecycle:
    def test_submit_approve(self, admin, world):
        draft = _draft(admin, world)
        no_changes = admin.post(f"{BASE}{draft['uid']}/submit/", {}, format="json")
        assert no_changes.status_code == 409 and no_changes.json()["code"] == "no_changes"
        admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"marketRates": {"ongrid_value": {"3": 239000}}}}, format="json")
        submitted = admin.post(f"{BASE}{draft['uid']}/submit/", {}, format="json").json()
        assert submitted["status"] == "SUBMITTED" and submitted["submitted_by"] is not None
        again = admin.post(f"{BASE}{draft['uid']}/submit/", {}, format="json")
        assert again.status_code == 409 and again.json()["code"] == "already_submitted"
        stale = admin.post(f"{BASE}{draft['uid']}/approve/", {"expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        approved = admin.post(f"{BASE}{draft['uid']}/approve/", {"note": "ok"}, format="json").json()
        assert approved["status"] == "APPROVED" and approved["note"] == "ok"
        world["version"].refresh_from_db()
        assert world["version"].status == ConfigStatus.SUPERSEDED and world["version"].superseded_at is not None
        assert OutboxEvent.objects.filter(event_type="packs.config_approved").exists()
        assert admin.post(f"{BASE}{draft['uid']}/submit/", {}, format="json").json()["code"] == "version_not_draft"

    def test_direct_approve_and_reject(self, admin, world):
        draft = _draft(admin, world)
        assert admin.post(f"{BASE}{draft['uid']}/approve/", {}, format="json").json()["code"] == "version_not_submitted"
        assert admin.post(f"{BASE}{draft['uid']}/approve/", {"direct": True}, format="json").json()["code"] == "no_changes"
        assert admin.post(f"{BASE}{draft['uid']}/reject/", {"reason": "no"}, format="json").json()["code"] == "version_not_submitted"
        admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"marketRates": {"ongrid_value": {"3": 239000}}}}, format="json")
        admin.post(f"{BASE}{draft['uid']}/submit/", {}, format="json")
        blank = admin.post(f"{BASE}{draft['uid']}/reject/", {"reason": "  "}, format="json")
        assert blank.status_code == 400
        rejected = admin.post(f"{BASE}{draft['uid']}/reject/", {"reason": "price too high"}, format="json").json()
        assert rejected["status"] == "REJECTED" and rejected["rejection_reason"] == "price too high"
        second = admin.post(BASE, {"based_on_uid": rejected["uid"]}, format="json").json()
        assert second["config"]["marketRates"] == {"ongrid_value": {"3": 239000}}
        admin.patch(f"{BASE}{second['uid']}/", {"sections": {"marketRates": {"ongrid_value": {"3": 235000}}}}, format="json")
        direct = admin.post(f"{BASE}{second['uid']}/approve/", {"direct": True}, format="json").json()
        assert direct["status"] == "APPROVED" and direct["submitted_by"] == direct["approved_by"]

    def test_run_checker_stores_a_run(self, editor, world):
        response = editor.post(f"{BASE}{world['version'].uid}/run-checker/", {}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["subject_type"] == "PACK_CONFIG_VERSION" and body["result"] == "WARN"
        assert set(body["summary"]["scopes"]) == {"ongrid-value-3", "ongrid-value-3-up5", "ongrid-value-5"}
        assert all(finding["context"]["pack"] for finding in body["findings"])
        assert Run.objects.count() == 1

    def test_run_checker_without_active_rule_set(self, admin, world):
        from engineering.models import RuleSet

        RuleSet.objects.update(active=False)
        response = admin.post(f"{BASE}{world['version'].uid}/run-checker/", {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "no_active_rule_set"

    def test_run_checker_on_history_is_409(self, admin, world):
        old = ConfigVersion.objects.create(number=9, status=ConfigStatus.SUPERSEDED)
        assert admin.post(f"{BASE}{old.uid}/run-checker/", {}, format="json").json()["code"] == "version_has_no_config"

    def test_mirror_lines_are_rebuilt_not_duplicated(self, admin, world):
        draft = _draft(admin, world)
        before = ConfigLine.objects.filter(pack__config_version__uid=draft["uid"]).count()
        admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"marketRates": {"ongrid_value": {"3": 1}}}}, format="json")
        assert ConfigLine.objects.filter(pack__config_version__uid=draft["uid"]).count() == before


class TestReviewFixes:
    def test_costs_and_margin_need_pricing_internal(self, viewer, admin, world):
        """PLAN §3.2: `pricing_internal` unlocks margin and cost fields on packs responses (Flarize refused pack-config reads to Sales)."""
        detail = viewer.get(f"{BASE}{world['version'].uid}/").json()
        assert detail["config"]["costs"] is None and detail["config"]["pricing"] is None
        assert detail["config"]["marketRates"] == {"ongrid_value": {"3": 229000}}
        internal = admin.get(f"{BASE}{world['version'].uid}/").json()
        assert internal["config"]["costs"]["office"] == 5500 and internal["config"]["pricing"]["marginPct"] == 20

    def test_costs_are_redacted_in_write_responses_too(self, editor, world):
        draft = editor.post(BASE, {}, format="json").json()
        assert draft["config"]["costs"] is None and draft["config"]["pricing"] is None
        patched = editor.patch(f"{BASE}{draft['uid']}/", {"sections": {"marketRates": {"ongrid_value": {"3": 1}}}}, format="json").json()
        assert patched["config"]["costs"] is None and patched["config"]["marketRates"] == {"ongrid_value": {"3": 1}}
        assert ConfigVersion.objects.get(uid=draft["uid"]).config["costs"]["office"] == 5500  # stored untouched

    def test_a_pack_removed_and_offered_again_keeps_its_pins(self, admin, world):
        draft = _draft(admin, world)
        url = f"{BASE}{draft['uid']}/packs/ongrid-value-5/"
        assert admin.put(url, {"pins": {"panel": str(world["pnl2"].uid)}}, format="json").status_code == 200
        templates = dict(draft["config"]["bomTemplates"])
        templates["ongrid"] = {**templates["ongrid"], "sizes": {"3": "3 kW"}}
        removed = admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"bomTemplates": templates}}, format="json")
        assert removed.status_code == 200, removed.json()
        assert admin.get(url).status_code == 404
        admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"bomTemplates": draft["config"]["bomTemplates"]}}, format="json")
        back = admin.get(url).json()
        assert back["panel"]["sku"] == "pnl2" and [pin["slot_key"] for pin in back["pins"]] == ["panel"]
        assert ConfigPack.all_objects.filter(config_version__uid=draft["uid"], key="ongrid-value-5").count() == 1

    def test_a_draft_identical_to_the_approved_version_is_not_submitted(self, admin, world):
        """Flarize ``submitDraft`` / ``approveDraftDirect``: NO_CHANGES compares with the approved configuration."""
        draft = _draft(admin, world)
        approved_rates = draft["config"]["marketRates"]
        admin.patch(f"{BASE}{draft['uid']}/", {"sections": {"marketRates": {"ongrid_value": {"3": 239000}}}}, format="json")
        admin.post(f"{BASE}{draft['uid']}/submit/", {}, format="json")
        rejected = admin.post(f"{BASE}{draft['uid']}/reject/", {"reason": "too high"}, format="json").json()
        second = admin.post(BASE, {"based_on_uid": rejected["uid"]}, format="json").json()
        # unchanged from the rejected proposal but different from the approved version: it may be proposed again
        resubmitted = admin.post(f"{BASE}{second['uid']}/submit/", {}, format="json")
        assert resubmitted.status_code == 200, resubmitted.json()
        # back to exactly the approved configuration: nothing to submit or approve
        admin.patch(f"{BASE}{second['uid']}/", {"sections": {"marketRates": approved_rates}}, format="json")
        same = admin.post(f"{BASE}{second['uid']}/submit/", {}, format="json")
        assert same.status_code == 409 and same.json()["code"] == "no_changes"
        direct = admin.post(f"{BASE}{second['uid']}/approve/", {"direct": True}, format="json")
        assert direct.status_code == 409 and direct.json()["code"] == "no_changes"
