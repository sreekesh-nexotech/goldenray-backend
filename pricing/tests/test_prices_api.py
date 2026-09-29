"""pricing/prices/ and pricing/current/ — append-only rows, one current per kind, internal gating, concurrency."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from audit.models import AuditLog
from catalog.models import ComponentStatus
from catalog.tests.factories import ComponentFactory
from pricing.models import CurrentPrice, Price, PriceKind, PriceSource
from pricing.services.prices import write_price
from pricing.tests.factories import PriceFactory
from procurement.tests.factories import SupplierFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/pricing/prices/"
CURRENT = "/api/v1/pricing/current/"


def body(component, **extra):
    return {"component_uid": str(component.uid), "kind": "LIST", "amount": "13585.00", **extra}


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        assert api_client.get(URL).status_code == 401
        assert api_client.post(URL, {}, format="json").status_code == 401
        assert api_client.get(CURRENT).status_code == 401

    def test_missing_permission_is_403(self, outsider, viewer):
        component = ComponentFactory()
        assert outsider.get(URL).status_code == 403
        assert outsider.get(CURRENT).status_code == 403
        assert viewer.get(URL).status_code == 200
        assert viewer.post(URL, body(component), format="json").status_code == 403

    def test_landed_rows_need_pricing_internal(self, editor, client):
        component = ComponentFactory()
        PriceFactory(component=component, kind=PriceKind.LANDED, amount=Decimal("9000"))
        PriceFactory(component=component, kind=PriceKind.PURCHASE, amount=Decimal("8900"))
        PriceFactory(component=component, kind=PriceKind.LIST, amount=Decimal("13585"))
        assert [row["kind"] for row in editor.get(URL).json()["results"]] == ["LIST"]
        assert [row["kind"] for row in editor.get(CURRENT).json()["results"]] == ["LIST"]
        assert sorted(row["kind"] for row in client.get(URL).json()["results"]) == ["LANDED", "LIST", "PURCHASE"]
        response = editor.post(URL, body(component, kind="LANDED"), format="json")
        assert response.status_code == 403 and response.json()["code"] == "pricing_internal_required"
        assert client.post(URL, body(component, kind="LANDED", amount="9100"), format="json").status_code == 201

    def test_scope_all(self, auth_client, make_user):
        PriceFactory()
        client = auth_client(make_user(grants={"pricing": ["view"]}, scopes={"pricing": "all"}))
        assert client.get(URL).json()["count"] == 1


class TestManualPrice:
    def test_create_closes_the_previous_row(self, client, pricing_user):
        component = ComponentFactory()
        old = PriceFactory(component=component, amount=Decimal("13000"), effective_from=date(2026, 1, 1))
        response = client.post(URL, body(component, effective_from=str(timezone.localdate()), note="new list"), format="json")
        assert response.status_code == 201, response.json()
        data = response.json()
        assert data["amount"] == "13585.00" and data["source"] == "MANUAL" and data["is_current"] is True
        old.refresh_from_db()
        assert old.effective_to == timezone.localdate() and old.version == 2
        assert Price.objects.filter(component=component, kind="LIST", effective_to__isnull=True).count() == 1
        assert AuditLog.objects.get(action="pricing.price_set").actor == pricing_user
        current = client.get(CURRENT, {"sku": component.sku}).json()["results"]
        assert [row["amount"] for row in current] == ["13585.00"]

    @pytest.mark.parametrize(
        "extra,field",
        [({"kind": "PURCHASE"}, "kind"), ({"amount": "-1"}, "amount"), ({"component_uid": "00000000-0000-0000-0000-000000000000"}, "component_uid"), ({"per_watt": "-2"}, "per_watt")],
    )
    def test_validation_envelope(self, client, extra, field):
        response = client.post(URL, body(ComponentFactory(), **extra), format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and field in response.json()["errors"]

    def test_future_and_backdated_rows_are_refused(self, client):
        component = ComponentFactory()
        PriceFactory(component=component, effective_from=date(2026, 3, 1))
        future = client.post(URL, body(component, effective_from=str(timezone.localdate() + timedelta(days=1))), format="json")
        assert future.status_code == 400 and future.json()["code"] == "effective_from_in_future"
        earlier = client.post(URL, body(component, effective_from="2026-02-01"), format="json")
        assert earlier.status_code == 400 and earlier.json()["code"] == "effective_from_before_current"

    def test_stale_current_uid_is_409(self, client):
        component = ComponentFactory()
        current = PriceFactory(component=component)
        response = client.post(URL, body(component, expected_current_uid="11111111-1111-1111-1111-111111111111"), format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        assert client.post(URL, body(component, expected_current_uid=str(current.uid)), format="json").status_code == 201
        other = ComponentFactory()
        assert client.post(URL, body(other, expected_current_uid=None), format="json").status_code == 201

    def test_retired_component_gets_no_price(self, client):
        response = client.post(URL, body(ComponentFactory(status=ComponentStatus.RETIRED)), format="json")
        assert response.status_code == 409 and response.json()["code"] == "component_retired"

    def test_supplier_and_filters(self, client):
        component = ComponentFactory()
        supplier = SupplierFactory()
        assert client.post(URL, body(component, supplier_uid=str(supplier.uid)), format="json").json()["supplier"]["code"] == supplier.code
        PriceFactory(component=ComponentFactory(), kind=PriceKind.LANDED)
        assert client.get(URL, {"kind": "LANDED"}).json()["count"] == 1
        assert client.get(URL, {"component": str(component.uid)}).json()["count"] == 1
        assert client.get(URL, {"current": "true", "supplier": str(supplier.uid)}).json()["count"] == 1
        assert client.get(URL, {"effective_on": str(timezone.localdate())}).json()["count"] == 2
        assert client.get(f"{URL}{Price.objects.first().uid}/").status_code == 200

    def test_list_query_budget(self, client, django_assert_max_num_queries):
        for _ in range(15):
            PriceFactory(supplier=SupplierFactory())
        with django_assert_max_num_queries(12):
            assert client.get(URL, {"page_size": 50}).json()["count"] == 15
        with django_assert_max_num_queries(12):
            assert client.get(CURRENT, {"page_size": 50}).json()["count"] == 15


class TestInvariants:
    def test_one_current_row_per_kind_is_a_database_constraint(self):
        component = ComponentFactory()
        PriceFactory(component=component)
        with pytest.raises(IntegrityError), transaction.atomic():
            PriceFactory(component=component)
        PriceFactory(component=component, kind=PriceKind.LANDED)

    def test_list_price_is_never_markup(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            PriceFactory(source=PriceSource.MARKUP)
        from core.errors import DomainError

        with pytest.raises(DomainError) as caught, transaction.atomic():
            write_price(ComponentFactory(), PriceKind.LIST, Decimal("1"), user=None, source=PriceSource.MARKUP)
        assert caught.value.code == "list_markup_forbidden"

    def test_rows_are_append_only_in_the_database(self):
        row = PriceFactory()
        with pytest.raises(Exception), transaction.atomic():
            Price.all_objects.filter(pk=row.pk).update(amount=Decimal("1"))
        with pytest.raises(Exception), transaction.atomic():
            Price.all_objects.filter(pk=row.pk).delete()
        Price.all_objects.filter(pk=row.pk).update(effective_to=date(2026, 2, 1))
        with pytest.raises(Exception), transaction.atomic():
            Price.all_objects.filter(pk=row.pk).update(effective_to=None)

    def test_current_price_view_is_the_open_row(self):
        component = ComponentFactory()
        PriceFactory(component=component, amount=Decimal("1"), effective_from=date(2026, 1, 1), effective_to=date(2026, 2, 1))
        open_row = PriceFactory(component=component, amount=Decimal("2"), effective_from=date(2026, 2, 1))
        rows = list(CurrentPrice.objects.filter(component=component))
        assert [(row.id, row.amount) for row in rows] == [(open_row.pk, Decimal("2.00"))]
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pricing_current_price")
            assert cursor.fetchone()[0] == 1

    def test_version_key_is_unique_per_kind(self):
        from core.errors import Conflict

        component = ComponentFactory()
        with transaction.atomic():
            write_price(component, PriceKind.LANDED, Decimal("1"), user=None, source=PriceSource.BATCH, version_key="B-1::x")
        with pytest.raises(Conflict) as caught, transaction.atomic():
            write_price(ComponentFactory(), PriceKind.LANDED, Decimal("1"), user=None, source=PriceSource.BATCH, version_key="B-1::x")
        assert caught.value.code == "price_version_exists"
