"""emi/* staff endpoints (module emi: view reads, edit writes): auth, grants, scope, validation, conflicts, versions."""

import pytest

from audit.models import AuditLog
from emi.models import Bank, EmiSettings
from emi.tests import factories

pytestmark = pytest.mark.django_db

# key → (entity, factory, create payload, factory kwargs clashing with the payload or None, (invalid payload, error field))
LISTS = {
    "banks": (
        "bank",
        factories.BankFactory,
        {"name": "State Bank of India", "abbr": "SBI", "slug": "sbi", "annual_rate": "0.0565", "max_loan": "600000.00", "features": ["Zero processing fee"]},
        {"slug": "sbi"},
        ({"name": "X", "abbr": "X", "annual_rate": "0.05", "min_loan": "10", "max_loan": "5"}, "max_loan"),
    ),
    "interest-rules": (
        "interest_rule",
        factories.InterestRateRuleFactory,
        {"label": "Loan up to ₹2L", "max_amount": "200000.00", "annual_rate": "0.0575", "min_annual_rate": "0.0575", "is_locked": True, "priority": 20},
        None,
        ({"label": "Bad", "annual_rate": "0.05", "min_annual_rate": "0.06"}, "annual_rate"),
    ),
    "subsidy-rules": (
        "subsidy_rule",
        factories.SubsidyRuleFactory,
        {"label": "PM Surya Ghar — 3kW and above", "kw_from": "3.00", "amount": "78000.00", "priority": 10},
        None,
        ({"label": "Bad", "kw_from": "5", "kw_to": "3", "amount": "1"}, "kw_to"),
    ),
    "system-sizes": (
        "system_size",
        factories.SystemSizeFactory,
        {"label": "3kW", "capacity_kw": "3.00", "price_per_kw": "76667.00", "price_min": "180000.00", "price_max": "500000.00", "monthly_bill_reference": "6000.00"},
        {"capacity_kw": "3.00"},
        ({"label": "x", "capacity_kw": "4", "price_per_kw": "1", "price_min": "9", "price_max": "5"}, "price_max"),
    ),
}
KEYS = list(LISTS)


def url(key, row=None):
    return f"/api/v1/emi/{key}/" + (f"{row.uid}/" if row is not None else "")


@pytest.fixture
def editor(auth_client, make_user):
    return auth_client(make_user(grants={"emi": ["view", "edit"]}))


@pytest.mark.parametrize("key", KEYS)
class TestEmiLists:
    def test_anonymous_is_401(self, api_client, key):
        row = LISTS[key][1]()
        assert api_client.get(url(key)).status_code == 401
        assert api_client.post(url(key), {}, format="json").status_code == 401
        assert api_client.delete(url(key, row)).status_code == 401

    def test_grants(self, auth_client, make_user, key):
        row = LISTS[key][1]()
        viewer = auth_client(make_user(grants={"emi": ["view"]}))
        assert viewer.get(url(key)).status_code == 200 and viewer.get(url(key, row)).status_code == 200
        assert viewer.post(url(key), LISTS[key][2], format="json").status_code == 403
        assert viewer.patch(url(key, row), {"is_active": False}, format="json").status_code == 403
        assert viewer.delete(url(key, row)).status_code == 403
        assert auth_client(make_user(grants={"reference_data": "*"})).get(url(key)).status_code == 403

    def test_scope_is_all_and_archived_rows_are_hidden(self, auth_client, make_user, key):
        factory = LISTS[key][1]
        factory.create_batch(2)
        factory().soft_delete()
        assert auth_client(make_user(grants={"emi": ["view"]})).get(url(key)).json()["count"] == 2

    def test_create_update_delete_are_audited(self, editor, key):
        entity, _factory, payload, _clash, _invalid = LISTS[key]
        created = editor.post(url(key), payload, format="json")
        assert created.status_code == 201, created.json()
        uid = created.json()["uid"]
        assert "id" not in created.json() and AuditLog.objects.filter(action=f"emi.{entity}_created", object_uid=uid).exists()
        patched = editor.patch(f"{url(key)}{uid}/", {"is_active": False, "expected_version": 1}, format="json")
        assert patched.status_code == 200 and patched.json()["version"] == 2
        assert AuditLog.objects.filter(action=f"emi.{entity}_updated", object_uid=uid).exists()
        assert editor.delete(f"{url(key)}{uid}/?expected_version=2").status_code == 204
        assert AuditLog.objects.filter(action=f"emi.{entity}_deleted", object_uid=uid).exists()

    def test_validation_error_envelope(self, editor, key):
        invalid, field = LISTS[key][4]
        response = editor.post(url(key), invalid, format="json")
        assert response.status_code == 400
        assert response.json()["code"] == "validation_error" and field in response.json()["errors"]

    def test_stale_version(self, editor, key):
        row = LISTS[key][1]()
        assert editor.patch(url(key, row), {"is_active": False, "expected_version": 1}, format="json").status_code == 200
        stale = editor.patch(url(key, row), {"is_active": True, "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
        assert editor.delete(url(key, row) + "?expected_version=1").status_code == 409

    def test_list_does_not_grow_queries_with_rows(self, editor, key, django_assert_max_num_queries):
        LISTS[key][1].create_batch(12)
        with django_assert_max_num_queries(12):
            response = editor.get(url(key))
        assert response.json()["count"] == 12


@pytest.mark.parametrize("key", [key for key in KEYS if LISTS[key][3]])  # rules have no natural key
def test_live_duplicate_is_409(editor, key):
    entity, factory, payload, clash, _invalid = LISTS[key]
    factory(**clash)
    response = editor.post(url(key), payload, format="json")
    assert response.status_code == 409 and response.json()["code"] == f"{entity}_exists"


class TestBanks:
    def test_slug_is_derived_when_missing_or_blank(self, editor):
        factories.BankFactory(slug="union-bank")
        created = editor.post("/api/v1/emi/banks/", {"name": "Union Bank", "abbr": "UBI", "annual_rate": "0.079"}, format="json").json()
        assert created["slug"] == "union-bank-2"
        bank = Bank.objects.get(uid=created["uid"])
        patched = editor.patch(f"/api/v1/emi/banks/{bank.uid}/", {"slug": "", "name": "Federal Bank"}, format="json").json()
        assert patched["slug"] == "federal-bank"

    def test_features_are_trimmed_and_blank_bullets_refused(self, editor):
        created = editor.post("/api/v1/emi/banks/", {"name": "B", "abbr": "B", "annual_rate": "0.08", "features": ["  Fast  ", "Cheap"]}, format="json").json()
        assert created["features"] == ["Fast", "Cheap"]
        refused = editor.post("/api/v1/emi/banks/", {"name": "C", "abbr": "C", "annual_rate": "0.08", "features": ["Fast", " "]}, format="json")
        assert refused.status_code == 400 and "features" in refused.json()["errors"]

    def test_rates_are_fractions(self, editor):
        response = editor.post("/api/v1/emi/banks/", {"name": "B", "abbr": "B", "annual_rate": "8.5"}, format="json")
        assert response.status_code == 400 and "annual_rate" in response.json()["errors"]

    def test_bad_colour_is_refused(self, editor):
        response = editor.post("/api/v1/emi/banks/", {"name": "B", "abbr": "B", "annual_rate": "0.08", "logo_bg": "blue"}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error"


class TestSettings:
    URL = "/api/v1/emi/settings/"

    def test_anonymous_and_grants(self, api_client, auth_client, make_user):
        assert api_client.get(self.URL).status_code == 401
        viewer = auth_client(make_user(grants={"emi": ["view"]}))
        assert viewer.get(self.URL).status_code == 200
        assert viewer.patch(self.URL, {"panel_life_years": 20}, format="json").status_code == 403
        assert auth_client(make_user(grants={"reference_data": "*"})).get(self.URL).status_code == 403

    def test_defaults_are_served_without_writing(self, editor):
        body = editor.get(self.URL).json()
        assert body["uid"] is None and body["version"] == 1 and body["down_payment_min_pct"] == "0.1000" and body["updated_at"] is None
        assert not EmiSettings.objects.exists()

    def test_first_edit_creates_the_row_and_audits(self, editor):
        response = editor.patch(self.URL, {"tenure_default_years": 7, "down_payment_quick_adds": ["10000", "5000", "5000"], "expected_version": 1}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["version"] == 2 and body["tenure_default_years"] == 7 and body["down_payment_quick_adds"] == ["5000.00", "10000.00"]
        assert AuditLog.objects.filter(action="emi.settings_created").exists() and AuditLog.objects.filter(action="emi.settings_updated").exists()

    def test_stale_version(self, editor):
        assert editor.patch(self.URL, {"panel_life_years": 20, "expected_version": 1}, format="json").status_code == 200
        stale = editor.patch(self.URL, {"panel_life_years": 21, "expected_version": 1}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"

    @pytest.mark.parametrize(
        ("payload", "field"),
        [
            ({"tenure_min_years": 6, "tenure_max_years": 5}, "tenure_max_years"),
            ({"tenure_default_years": 11}, "tenure_default_years"),
            ({"down_payment_min_pct": "0.95"}, "down_payment_max_pct"),
            ({"down_payment_min_pct": "0"}, "down_payment_min_pct"),
            ({"daily_saving_divisor": 0}, "daily_saving_divisor"),
            ({"rate_max": "18"}, "rate_max"),
            ({"down_payment_quick_adds": ["-5"]}, "down_payment_quick_adds"),
        ],
    )
    def test_validation_error_envelope(self, editor, payload, field):
        response = editor.patch(self.URL, payload, format="json")
        assert response.status_code == 400 and field in response.json()["errors"]
        assert not EmiSettings.objects.exists()
