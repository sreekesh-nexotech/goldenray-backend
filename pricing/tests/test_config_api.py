"""pricing/cost-config/ (+ history), pricing/installation-matrix/, pricing/statutory-fees/, pricing/validity-policy/."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import AuditLog
from pricing.models import CostConfig, InstallationMatrix, StatutoryFee, ValidityKind, ValidityPolicy
from pricing.services import validity
from pricing.tests.factories import CostConfigFactory, InstallationMatrixFactory, StatutoryFeeFactory

pytestmark = pytest.mark.django_db
CONFIG = "/api/v1/pricing/cost-config/"
HISTORY = "/api/v1/pricing/cost-config/history/"
MATRIX = "/api/v1/pricing/installation-matrix/"
FEES = "/api/v1/pricing/statutory-fees/"
VALIDITY = "/api/v1/pricing/validity-policy/"


class TestCostConfig:
    def test_permissions(self, api_client, outsider, viewer):
        for url in (CONFIG, HISTORY):
            assert api_client.get(url).status_code == 401
            assert outsider.get(url).status_code == 403
            assert viewer.get(url).status_code == 200
        assert viewer.put(CONFIG, {"entries": [{"key": "install_rate", "value": 3000}]}, format="json").status_code == 403

    def test_put_writes_effective_rows_and_closes_previous(self, client, pricing_user):
        old = CostConfigFactory(key="install_rate", value=2500, effective_from=date(2026, 1, 1))
        response = client.put(CONFIG, {"entries": [{"key": "install_rate", "value": 3000}, {"key": "service_years", "value": 5}], "note": "2026 rates"}, format="json")
        assert response.status_code == 200, response.json()
        assert {row["key"] for row in response.json()["written"]} == {"install_rate", "service_years"}
        old.refresh_from_db()
        assert old.effective_to == timezone.localdate()
        current = {row["key"]: row["value"] for row in client.get(CONFIG).json()["current"]}
        assert current == {"install_rate": 3000, "service_years": 5}
        again = client.put(CONFIG, {"entries": [{"key": "install_rate", "value": 3000}]}, format="json")
        assert again.json()["written"] == []
        assert AuditLog.objects.filter(action="pricing.cost_config_set").count() == 2
        assert client.get(HISTORY, {"key": "install_rate"}).json()["count"] == 2

    @pytest.mark.parametrize(
        "entry,code",
        [
            ({"key": "install_rate", "value": -1}, "validation_error"),
            ({"key": "install_rate", "value": "abc"}, "validation_error"),
            ({"key": "service_years", "value": 2.5}, "validation_error"),
            ({"key": "structure_repair_pct", "value": 10}, "validation_error"),
            ({"key": "unknown_key", "value": 1}, "validation_error"),
            ({"key": "target_gross_margin_by_tier", "value": {"BASE": {"target": 1.2}}}, "validation_error"),
            ({"key": "cost_engine.config", "value": {"schema": "nope"}}, "validation_error"),
            ({"key": "energy.config", "value": {"regions": {}}}, "validation_error"),
        ],
    )
    def test_values_are_validated_per_key(self, client, entry, code):
        response = client.put(CONFIG, {"entries": [entry]}, format="json")
        assert response.status_code == 400 and response.json()["code"] == code

    def test_stale_current_uid(self, client):
        row = CostConfigFactory(key="miscellaneous", value=1000)
        stale = client.put(CONFIG, {"entries": [{"key": "miscellaneous", "value": 900, "current_uid": "11111111-1111-1111-1111-111111111111"}]}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        ok = client.put(CONFIG, {"entries": [{"key": "miscellaneous", "value": 900, "current_uid": str(row.uid)}]}, format="json")
        assert ok.status_code == 200

    def test_backdated_value_is_refused(self, client):
        CostConfigFactory(key="miscellaneous", value=1000, effective_from=date(2026, 6, 1))
        response = client.put(CONFIG, {"entries": [{"key": "miscellaneous", "value": 900}], "effective_from": "2026-05-01"}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "effective_from_before_current"

    def test_margin_is_hidden_without_pricing_internal(self, editor, client):
        CostConfigFactory(key="target_gross_margin_by_tier", value={"BASE": {"target": 0.2}})
        CostConfigFactory(key="cost_engine.config", value={"schema": "flarize.cost-config/2", "margin": {"tiers": {}}, "gst": {}})
        rows = {row["key"]: row for row in editor.get(CONFIG).json()["current"]}
        assert rows["target_gross_margin_by_tier"]["value"] is None and rows["target_gross_margin_by_tier"]["hidden"] is True
        assert "margin" not in rows["cost_engine.config"]["value"] and rows["cost_engine.config"]["hidden"] is False
        full = {row["key"]: row for row in client.get(CONFIG).json()["current"]}
        assert full["target_gross_margin_by_tier"]["value"] == {"BASE": {"target": 0.2}}
        response = editor.put(CONFIG, {"entries": [{"key": "target_gross_margin_by_tier", "value": {"BASE": {"target": 0.25}}}]}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "pricing_internal_required"
        assert client.put(CONFIG, {"entries": [{"key": "target_gross_margin_by_tier", "value": {"BASE": {"target": 0.25, "minimum": 0.15}}}]}, format="json").status_code == 200

    def test_one_current_row_per_key_is_a_constraint(self):
        CostConfigFactory(key="install_rate")
        with pytest.raises(IntegrityError), transaction.atomic():
            CostConfigFactory(key="install_rate")

    def test_documents_accept_the_real_flarize_files(self, client):
        from pricing.tests.legacy_fixtures import flarize_file

        entries = [
            {"key": key, "value": flarize_file(name)} for key, name in (("energy.config", "energy-config.json"), ("finance.config", "finance-config.json"), ("rate_card", "project-rate-card.json"))
        ]
        assert client.put(CONFIG, {"entries": entries}, format="json").status_code == 200

    def test_read_query_budget(self, client, pricing_user, django_assert_max_num_queries):
        keys = ["install_rate", "service_rate_year", "transport_rate_per_km", "miscellaneous", "office_per_project", "elevated_structure_rate", "sheet_structure_rate", "structure_labor"]
        for number, key in enumerate(keys):
            CostConfigFactory(key=key, value=100 + number, effective_from=date(2025, 1, 1), effective_to=date(2026, 1, 1), created_by=pricing_user)
            CostConfigFactory(key=key, value=200 + number, created_by=pricing_user)
        with django_assert_max_num_queries(10):
            assert client.get(HISTORY, {"page_size": 50}).json()["count"] == 16
        with django_assert_max_num_queries(10):
            assert client.get(CONFIG).status_code == 200


class TestInstallationMatrix:
    def test_permissions(self, api_client, outsider, viewer):
        row = InstallationMatrixFactory()
        assert api_client.get(MATRIX).status_code == 401
        assert outsider.get(MATRIX).status_code == 403
        assert viewer.get(MATRIX).status_code == 200
        assert viewer.post(MATRIX, {"size_kw": "3", "install_cost": "1"}, format="json").status_code == 403
        assert viewer.delete(f"{MATRIX}{row.uid}/").status_code == 403

    def test_crud(self, client):
        response = client.post(MATRIX, {"size_kw": "5", "phase": "1P", "installation_type": "SHEET", "install_cost": "26000"}, format="json")
        assert response.status_code == 201 and response.json()["size_key"] == "5sp"
        uid = response.json()["uid"]
        duplicate = client.post(MATRIX, {"size_kw": "5", "phase": "1P", "installation_type": "SHEET", "install_cost": "1"}, format="json")
        assert duplicate.status_code == 409 and duplicate.json()["code"] == "installation_cell_exists"
        updated = client.patch(f"{MATRIX}{uid}/", {"install_cost": "27000", "expected_version": 1}, format="json")
        assert updated.status_code == 200 and updated.json()["install_cost"] == "27000.00" and updated.json()["version"] == 2
        stale = client.patch(f"{MATRIX}{uid}/", {"install_cost": "1", "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        assert client.post(MATRIX, {"size_kw": "0", "install_cost": "1"}, format="json").status_code == 400
        assert client.delete(f"{MATRIX}{uid}/").status_code == 204
        assert not InstallationMatrix.objects.exists()

    def test_exact_cell_lookup(self):
        from pricing.services.masters import matrix_cost

        any_phase = InstallationMatrixFactory(size_kw=Decimal("5"), phase="", installation_type="FLAT", install_cost=Decimal("22000"))
        three = InstallationMatrixFactory(size_kw=Decimal("5"), phase="3P", installation_type="FLAT", install_cost=Decimal("23000"))
        assert matrix_cost(Decimal("5"), "FLAT", "3P") == three
        assert matrix_cost(Decimal("5"), "FLAT", "1P") == any_phase
        assert matrix_cost(Decimal("4"), "FLAT", "") is None

    def test_list_query_budget(self, client, django_assert_max_num_queries):
        for size in range(1, 16):
            InstallationMatrixFactory(size_kw=Decimal(size))
        with django_assert_max_num_queries(10):
            assert client.get(MATRIX, {"page_size": 50}).json()["count"] == 15


class TestStatutoryFees:
    def test_permissions(self, api_client, outsider, viewer):
        assert api_client.get(FEES).status_code == 401
        assert outsider.get(FEES).status_code == 403
        assert viewer.post(FEES, {}, format="json").status_code == 403

    def test_crud_and_one_current_fee_per_band(self, client):
        payload = {"kind": "KSEB_REGISTRATION", "label": "3 KW", "capacity_kw_max": "3", "amount": "5400"}
        response = client.post(FEES, payload, format="json")
        assert response.status_code == 201 and response.json()["effective_from"] == str(timezone.localdate())
        duplicate = client.post(FEES, payload, format="json")
        assert duplicate.status_code == 409 and duplicate.json()["code"] == "statutory_fee_exists"
        uid = response.json()["uid"]
        closed = client.patch(f"{FEES}{uid}/", {"effective_to": str(timezone.localdate()), "expected_version": 1}, format="json")
        assert closed.status_code == 200
        assert client.post(FEES, {**payload, "amount": "5600"}, format="json").status_code == 201
        assert client.get(FEES, {"current": "true"}).json()["count"] == 1
        bad = client.patch(f"{FEES}{uid}/", {"effective_to": str(timezone.localdate() - timedelta(days=4000))}, format="json")
        assert bad.status_code == 400
        assert client.post(FEES, {**payload, "kind": "NOPE"}, format="json").status_code == 400
        assert client.delete(f"{FEES}{uid}/").status_code == 204

    def test_null_phase_and_capacity_are_one_band(self):
        StatutoryFeeFactory(kind="NET_METER_TEST", label="Test", phase=None, capacity_kw_max=None)
        with pytest.raises(IntegrityError), transaction.atomic():
            StatutoryFeeFactory(kind="NET_METER_TEST", label="Test 2", phase=None, capacity_kw_max=None)
        assert StatutoryFee.objects.count() == 1

    def test_list_query_budget(self, client, django_assert_max_num_queries):
        for size in range(1, 16):
            StatutoryFeeFactory(capacity_kw_max=Decimal(size), label=f"{size} KW")
        with django_assert_max_num_queries(10):
            assert client.get(FEES, {"page_size": 50}).json()["count"] == 15


class TestValidityPolicy:
    def test_permissions(self, api_client, outsider, viewer):
        assert api_client.get(VALIDITY).status_code == 401
        assert outsider.get(VALIDITY).status_code == 403
        assert viewer.get(VALIDITY).status_code == 200
        assert viewer.put(VALIDITY, {"days": 15}, format="json").status_code == 403

    def test_default_and_windows(self, client):
        empty = client.get(VALIDITY).json()
        assert empty["days"] is None and empty["resolved_now"] is None
        created = client.put(VALIDITY, {"days": 15, "grace_days": 2}, format="json")
        assert created.status_code == 200 and created.json()["resolved_now"]["source"] == "DEFAULT"
        now = timezone.now()
        windows = [
            {"policy_id": "SEP", "days": 30, "effective_from": (now - timedelta(days=5)).isoformat(), "effective_to": (now + timedelta(days=5)).isoformat()},
            {"policy_id": "SEP2", "days": 7, "effective_from": (now - timedelta(days=1)).isoformat()},
        ]
        response = client.put(VALIDITY, {"windows": windows, "expected_version": 1}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["resolved_now"] == {"days": 7, "grace_days": 0, "source": "WINDOW", "policy_id": "SEP2", "version": ""}
        assert body["version"] == 2
        stale = client.put(VALIDITY, {"days": 20, "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        dropped = client.put(VALIDITY, {"windows": [windows[0]], "expected_version": 2}, format="json").json()
        assert dropped["resolved_now"]["policy_id"] == "SEP"
        assert ValidityPolicy.objects.get(policy_id="SEP2").status == "INACTIVE"

    def test_window_validation(self, client):
        now = timezone.now()
        response = client.put(
            VALIDITY, {"days": 15, "windows": [{"policy_id": "X", "days": 5, "effective_from": now.isoformat(), "effective_to": (now - timedelta(days=1)).isoformat()}]}, format="json"
        )
        assert response.status_code == 400
        assert not ValidityPolicy.objects.filter(kind=ValidityKind.WINDOW).exists()
        assert client.put(VALIDITY, {"grace_days": 1}, format="json").status_code == 400

    def test_resolution_tie_break_and_missing_policy(self):
        from core.errors import Conflict

        with pytest.raises(Conflict):
            validity.resolve()
        start = timezone.now() - timedelta(days=1)
        ValidityPolicy.objects.create(key="QUOTATION", kind="WINDOW", policy_id="A", days=10, effective_from=start)
        ValidityPolicy.objects.create(key="QUOTATION", kind="WINDOW", policy_id="B", days=20, effective_from=start)
        assert validity.resolve()["policy_id"] == "B"


def test_cost_config_rows_are_effective_dated():
    row = CostConfig.objects.create(key="install_rate", value=1, effective_from=date(2026, 1, 1))
    with pytest.raises(IntegrityError), transaction.atomic():
        CostConfig.objects.filter(pk=row.pk).update(effective_to=date(2025, 1, 1))
