"""calculators/* staff CRUD of the sizing tables (module reference_data): auth, grants, scope, validation, conflicts, versions."""

import pytest

from audit.models import AuditLog
from calculators.models import BillRangeSize, CapacitySize
from calculators.tests.factories import BillRangeSizeFactory, CapacitySizeFactory

pytestmark = pytest.mark.django_db

# key → (entity, factory, create payload, factory kwargs clashing with the payload, (invalid payload, error field))
LISTS = {
    "capacity-sizes": (
        "capacity_size",
        CapacitySizeFactory,
        {"power_capacity_kw": "4.500", "installation_days": 5, "total_cost": "306000.00", "total_subsidy": "78000.00", "area_required_sqft": 360},
        {"power_capacity_kw": "4.5"},
        ({"power_capacity_kw": "4", "installation_days": 5, "total_cost": "-1", "total_subsidy": "0", "area_required_sqft": 1}, "total_cost"),
    ),
    "bill-range-sizes": (
        "bill_range_size",
        BillRangeSizeFactory,
        {
            "bill_range": 12000,
            "property_type": "COMMERCIAL",
            "power_capacity_kw": "6.5",
            "installation_days_range": "3-8",
            "total_cost": "410000.00",
            "total_subsidy": "0.00",
            "area_required_sqft": 520,
            "loan_available": "N/A",
            "final_cost": "410000.00",
            "interest_rate": "0.0000",
        },
        {"bill_range": 12000, "property_type": "COMMERCIAL"},
        ({"bill_range": 1, "property_type": "INDUSTRIAL", "power_capacity_kw": "1", "installation_days_range": "1", "total_cost": "1", "total_subsidy": "0", "area_required_sqft": 1}, "property_type"),
    ),
}
KEYS = list(LISTS)


def url(key, row=None):
    return f"/api/v1/calculators/{key}/" + (f"{row.uid}/" if row is not None else "")


@pytest.fixture
def editor(auth_client, make_user):
    return auth_client(make_user(grants={"reference_data": "*"}))


@pytest.mark.parametrize("key", KEYS)
class TestSizingTables:
    def test_anonymous_is_401(self, api_client, key):
        row = LISTS[key][1]()
        assert api_client.get(url(key)).status_code == 401
        assert api_client.post(url(key), {}, format="json").status_code == 401
        assert api_client.patch(url(key, row), {}, format="json").status_code == 401
        assert api_client.delete(url(key, row)).status_code == 401

    def test_grants(self, auth_client, make_user, key):
        row = LISTS[key][1]()
        viewer = auth_client(make_user(grants={"reference_data": ["view"]}))
        assert viewer.get(url(key)).status_code == 200 and viewer.get(url(key, row)).status_code == 200
        assert viewer.post(url(key), LISTS[key][2], format="json").status_code == 403
        assert viewer.patch(url(key, row), {"is_active": False}, format="json").status_code == 403
        assert viewer.delete(url(key, row)).status_code == 403
        assert auth_client(make_user(grants={"reference_data": ["view", "edit"]})).delete(url(key, row)).status_code == 403
        assert auth_client(make_user(grants={"reference_data": ["view", "create"]})).patch(url(key, row), {"is_active": False}, format="json").status_code == 403
        assert auth_client(make_user(grants={"emi": "*"})).get(url(key)).status_code == 403

    def test_scope_is_all_and_archived_rows_are_hidden(self, auth_client, make_user, key):
        factory = LISTS[key][1]
        factory.create_batch(2)
        archived = factory()
        archived.soft_delete()
        client = auth_client(make_user(grants={"reference_data": ["view"]}))
        assert client.get(url(key)).json()["count"] == 2
        assert client.get(url(key, archived)).status_code == 404

    def test_create_update_delete_are_audited(self, editor, key):
        entity, _factory, payload, _clash, _invalid = LISTS[key]
        created = editor.post(url(key), payload, format="json")
        assert created.status_code == 201, created.json()
        body = created.json()
        assert body["version"] == 1 and "id" not in body
        uid = body["uid"]
        assert AuditLog.objects.filter(action=f"calculators.{entity}_created", object_uid=uid).exists()
        patched = editor.patch(f"{url(key)}{uid}/", {"is_active": False, "expected_version": 1}, format="json")
        assert patched.status_code == 200 and patched.json()["is_active"] is False and patched.json()["version"] == 2
        assert AuditLog.objects.filter(action=f"calculators.{entity}_updated", object_uid=uid).exists()
        assert editor.delete(f"{url(key)}{uid}/?expected_version=2").status_code == 204
        assert AuditLog.objects.filter(action=f"calculators.{entity}_deleted", object_uid=uid).exists()
        model = CapacitySize if key == "capacity-sizes" else BillRangeSize
        assert model.all_objects.get(uid=uid).deleted_at is not None

    def test_validation_error_envelope(self, editor, key):
        invalid, field = LISTS[key][4]
        response = editor.post(url(key), invalid, format="json")
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "validation_error" and field in body["errors"]

    def test_live_duplicate_is_409(self, editor, key):
        entity, factory, payload, clash, _invalid = LISTS[key]
        factory(**clash)
        response = editor.post(url(key), payload, format="json")
        assert response.status_code == 409 and response.json()["code"] == f"{entity}_exists"

    def test_duplicate_of_an_archived_row_is_allowed(self, editor, key):
        _entity, factory, payload, clash, _invalid = LISTS[key]
        factory(**clash).soft_delete()
        assert editor.post(url(key), payload, format="json").status_code == 201

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
        assert response.status_code == 200 and response.json()["count"] == 12


def test_bill_range_sizes_filter_by_property_type(editor):
    BillRangeSizeFactory(property_type="RESIDENTIAL")
    BillRangeSizeFactory(property_type="COMMERCIAL")
    body = editor.get("/api/v1/calculators/bill-range-sizes/?property_type=COMMERCIAL").json()
    assert body["count"] == 1 and body["results"][0]["property_type"] == "COMMERCIAL"


def test_interest_rate_is_a_fraction(editor):
    payload = {**LISTS["bill-range-sizes"][2], "interest_rate": "6.5"}
    response = editor.post("/api/v1/calculators/bill-range-sizes/", payload, format="json")
    assert response.status_code == 400 and "interest_rate" in response.json()["errors"]
