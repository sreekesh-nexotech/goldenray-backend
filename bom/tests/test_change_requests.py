"""``/api/v1/change-requests/`` over the BOM and pricing change kinds — submit, approve (applies), reject, cancel; permissions, scope, staleness."""

from decimal import Decimal

import pytest

from approvals.models import ChangeRequest
from audit.models import AuditLog
from bom.models import FixedItem
from bom.tests.factories import FixedItemFactory, TemplateFactory
from catalog.tests.factories import ComponentFactory
from core.models import OutboxEvent
from pricing.models import CostConfig, Price
from pricing.tests.factories import CostConfigFactory, PriceFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/change-requests/"
REQUESTER = {"change_requests": ["view", "create"], "bom": ["view"], "pricing": ["view"]}
APPROVER = {"change_requests": "*", "bom": "*", "pricing": "*", "pricing_internal": ["view"]}


def detail(change, suffix=""):
    uid = change["uid"] if isinstance(change, dict) else change.uid
    return f"{URL}{uid}/{suffix}"


@pytest.fixture
def requester_user(make_user):
    return make_user(grants=REQUESTER, scopes={"change_requests": "owned"})


@pytest.fixture
def requester(auth_client, requester_user):
    return auth_client(requester_user)


@pytest.fixture
def approver(auth_client, make_user):
    return auth_client(make_user(grants=APPROVER, scopes={"change_requests": "all"}))


def _ask_price(client, item, price="99.50", **extra):
    body = {"kind": "bom.fixed_item", "operation": "UPDATE", "target_uid": str(item.uid), "payload": {"unit_price": price, "expected_version": item.version}, "reason": "Supplier raised it", **extra}
    return client.post(URL, body, format="json")


class TestSubmit:
    def test_submit_changes_nothing_and_snapshots_the_row(self, requester, requester_user):
        item = FixedItemFactory(unit_price=Decimal("10.00"))
        response = _ask_price(requester, item)
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["status"] == "PENDING" and body["kind_label"] == "Fixed price item" and body["module"] == "bom"
        assert body["before"]["unit_price"] == "10.00" and body["payload"] == {"unit_price": "99.50"} and body["base_version"] == item.version
        assert body["requested_by"]["uid"] == str(requester_user.uid) and body["target_label"]
        assert FixedItem.objects.get(pk=item.pk).unit_price == Decimal("10.00")
        assert AuditLog.objects.filter(action="approvals.change_requested").exists()
        assert OutboxEvent.objects.filter(event_type="approvals.change_requested").exists()

    def test_payload_is_validated_on_submit(self, requester):
        response = _ask_price(requester, FixedItemFactory(), price="-5")
        assert response.status_code == 400 and "unit_price" in response.json()["errors"]
        assert not ChangeRequest.objects.exists()

    def test_one_pending_request_per_row(self, requester):
        item = FixedItemFactory()
        assert _ask_price(requester, item).status_code == 201
        second = _ask_price(requester, item, price="12")
        assert second.status_code == 409 and second.json()["code"] == "change_request_pending"

    def test_stale_expected_version_is_refused_on_submit(self, requester):
        item = FixedItemFactory()
        response = requester.post(
            URL, {"kind": "bom.fixed_item", "operation": "UPDATE", "target_uid": str(item.uid), "payload": {"unit_price": "1", "expected_version": item.version + 3}}, format="json"
        )
        assert response.status_code == 409 and response.json()["code"] == "stale_version"

    @pytest.mark.parametrize(
        ("body", "status", "code"),
        [
            ({"kind": "no.such", "operation": "CREATE"}, 400, "unknown_change_kind"),
            ({"kind": "bom.fixed_item", "operation": "SET"}, 400, "validation_error"),
            ({"kind": "bom.fixed_item", "operation": "UPDATE"}, 400, "validation_error"),
            ({"kind": "bom.fixed_item", "operation": "UPDATE", "target_uid": "00000000-0000-0000-0000-000000000000"}, 404, "change_target_missing"),
        ],
    )
    def test_submit_errors(self, requester, body, status, code):
        response = requester.post(URL, body, format="json")
        assert response.status_code == status and response.json()["code"] == code

    def test_permissions(self, api_client, auth_client, make_user):
        item = FixedItemFactory()
        assert api_client.get(URL).status_code == 401
        assert auth_client(make_user(grants={"bom": "*"})).get(URL).status_code == 403
        # May ask, but cannot see the module the change is for.
        blind = auth_client(make_user(grants={"change_requests": ["view", "create"]}))
        assert _ask_price(blind, item).status_code == 403
        # May only view: cannot ask.
        viewer = auth_client(make_user(grants={"change_requests": ["view"], "bom": ["view"]}))
        assert _ask_price(viewer, item).status_code == 403


class TestDecide:
    def test_approve_applies_the_change_as_the_approver(self, requester, approver):
        item = FixedItemFactory(unit_price=Decimal("10.00"))
        change = _ask_price(requester, item).json()
        response = approver.post(detail(change, "approve/"), {"note": "ok"}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["status"] == "APPROVED" and body["decided_by"] and body["decision_note"] == "ok" and body["result_uid"] == str(item.uid)
        assert FixedItem.objects.get(pk=item.pk).unit_price == Decimal("99.50")
        assert AuditLog.objects.filter(action="approvals.change_approved").exists()
        # The owning app's own audit row: applied through its service, not a raw write.
        assert AuditLog.objects.filter(object_uid=item.uid).exclude(action__startswith="approvals.").exists()
        again = approver.post(detail(change, "approve/"), {}, format="json")
        assert again.status_code == 409 and again.json()["code"] == "change_request_decided"

    def test_create_and_delete(self, auth_client, make_user, approver):
        requester = auth_client(make_user(grants=REQUESTER))
        template = TemplateFactory()
        body = {"template_uid": str(template.uid), "name": "Earthing kit", "unit_price": "450.00", "qty_rule": {"type": "size_table", "qty": {"3": 1}}}
        created = requester.post(URL, {"kind": "bom.fixed_item", "operation": "CREATE", "payload": body}, format="json").json()
        assert created["target_label"] == "Earthing kit" and not FixedItem.objects.filter(name="Earthing kit").exists()
        applied = approver.post(detail(created, "approve/"), {}, format="json").json()
        new_item = FixedItem.objects.get(name="Earthing kit")
        assert applied["result_uid"] == str(new_item.uid)
        removal = requester.post(URL, {"kind": "bom.fixed_item", "operation": "DELETE", "target_uid": str(new_item.uid)}, format="json").json()
        assert approver.post(detail(removal, "approve/"), {}, format="json").status_code == 200
        assert not FixedItem.objects.filter(pk=new_item.pk).exists() and FixedItem.all_objects.get(pk=new_item.pk).deleted_at

    def test_a_row_changed_since_the_request_is_not_overwritten(self, requester, approver):
        item = FixedItemFactory()
        change = _ask_price(requester, item).json()
        assert approver.patch(f"/api/v1/bom/fixed-items/{item.uid}/", {"name": "Edited meanwhile"}, format="json").status_code == 200
        response = approver.post(detail(change, "approve/"), {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        assert ChangeRequest.objects.get(uid=change["uid"]).status == "PENDING"
        assert FixedItem.objects.get(pk=item.pk).unit_price == Decimal("10.00")

    def test_never_ones_own(self, auth_client, make_user):
        both = auth_client(make_user(grants=APPROVER))
        change = _ask_price(both, FixedItemFactory()).json()
        response = both.post(detail(change, "approve/"), {}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "self_approval"

    def test_approver_needs_the_grant_applying_needs(self, requester, auth_client, make_user):
        change = _ask_price(requester, FixedItemFactory()).json()
        weak = auth_client(make_user(grants={"change_requests": "*", "bom": ["view"]}, scopes={"change_requests": "all"}))
        assert weak.post(detail(change, "approve/"), {}, format="json").status_code == 403
        assert requester.post(detail(change, "approve/"), {}, format="json").status_code == 403

    def test_reject_needs_a_note_and_changes_nothing(self, requester, approver):
        item = FixedItemFactory(unit_price=Decimal("10.00"))
        change = _ask_price(requester, item).json()
        assert approver.post(detail(change, "reject/"), {}, format="json").json()["code"] == "validation_error"
        response = approver.post(detail(change, "reject/"), {"note": "Price list not updated yet"}, format="json")
        assert response.status_code == 200 and response.json()["status"] == "REJECTED"
        assert FixedItem.objects.get(pk=item.pk).unit_price == Decimal("10.00")
        # The row is free for a new request once this one is decided.
        assert _ask_price(requester, item).status_code == 201

    def test_cancel_is_for_the_requester(self, requester, approver):
        change = _ask_price(requester, FixedItemFactory()).json()
        assert approver.post(detail(change, "cancel/"), {}, format="json").status_code == 403
        response = requester.post(detail(change, "cancel/"), {}, format="json")
        assert response.status_code == 200 and response.json()["status"] == "CANCELLED"

    def test_stale_request_version(self, requester, approver):
        change = _ask_price(requester, FixedItemFactory()).json()
        response = approver.post(detail(change, "approve/"), {"expected_version": change["version"] + 1}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"


class TestCostConfig:
    def test_cost_rates_change_only_on_approval(self, requester, approver):
        CostConfigFactory(key="install_rate", value=3000)
        current = CostConfig.objects.get(key="install_rate", effective_to__isnull=True)
        body = {"entries": [{"key": "install_rate", "value": 3500, "current_uid": str(current.uid)}], "note": "2026 labour rate"}
        change = requester.post(URL, {"kind": "pricing.cost_config", "operation": "SET", "payload": body}, format="json")
        assert change.status_code == 201, change.json()
        assert change.json()["before"]["current"][0]["value"] == 3000 and "install_rate" in change.json()["target_label"]
        assert CostConfig.objects.get(key="install_rate", effective_to__isnull=True).value == 3000
        assert approver.post(detail(change.json(), "approve/"), {}, format="json").status_code == 200
        assert CostConfig.objects.get(key="install_rate", effective_to__isnull=True).value == 3500

    def test_invalid_value_is_refused_on_submit(self, requester):
        response = requester.post(URL, {"kind": "pricing.cost_config", "operation": "SET", "payload": {"entries": [{"key": "no_such_key", "value": 1}]}}, format="json")
        assert response.status_code == 400


class TestListing:
    def test_owned_scope_sees_own_requests_and_approvers_see_all(self, requester, auth_client, make_user, approver):
        mine = _ask_price(requester, FixedItemFactory()).json()
        other = auth_client(make_user(grants=REQUESTER, scopes={"change_requests": "owned"}))
        theirs = _ask_price(other, FixedItemFactory()).json()
        assert [row["uid"] for row in requester.get(URL).json()["results"]] == [mine["uid"]]
        assert requester.get(detail(theirs)).status_code == 404
        assert {row["uid"] for row in approver.get(URL, {"status": "PENDING"}).json()["results"]} == {mine["uid"], theirs["uid"]}
        assert approver.get(URL, {"status": "APPROVED"}).json()["count"] == 0

    def test_kinds(self, requester):
        keys = {row["key"] for row in requester.get(f"{URL}kinds/").json()}
        assert {"bom.fixed_item", "bom.structure_item", "bom.tube_weight", "catalog.component", "pricing.cost_config", "pricing.price"} <= keys


class TestComponentPrice:
    """Manual prices are live as soon as written (the BOM quote reads the current LIST row), so they need approval too."""

    @staticmethod
    def _ask(client, component, amount="1500.00", **extra):
        payload = {"component_uid": str(component.uid), "kind": "LIST", "amount": amount, **extra}
        return client.post(URL, {"kind": "pricing.price", "operation": "CREATE", "payload": payload, "reason": "Supplier revised the list price"}, format="json")

    def test_a_price_is_live_only_after_approval(self, requester, approver):
        price = PriceFactory(amount="1000.00")
        response = self._ask(requester, price.component)
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["kind_label"] == "Component price" and price.component.sku in body["target_label"]
        assert body["before"]["amount"] == "1000.00" and body["payload"]["expected_current_uid"] == str(price.uid)
        assert Price.objects.get(component=price.component, kind="LIST", effective_to__isnull=True).amount == 1000
        assert approver.post(detail(body, "approve/"), {}, format="json").status_code == 200
        live = Price.objects.get(component=price.component, kind="LIST", effective_to__isnull=True)
        assert live.amount == Decimal("1500.00") and live.uid != price.uid

    def test_repriced_in_the_meantime_is_not_overwritten(self, requester, approver):
        price = PriceFactory(amount="1000.00")
        body = self._ask(requester, price.component).json()
        direct = approver.post("/api/v1/pricing/prices/", {"component_uid": str(price.component.uid), "kind": "LIST", "amount": "1200.00", "expected_current_uid": str(price.uid)}, format="json")
        assert direct.status_code == 201, direct.json()
        response = approver.post(detail(body, "approve/"), {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        assert Price.objects.get(component=price.component, kind="LIST", effective_to__isnull=True).amount == Decimal("1200.00")

    def test_first_price_of_a_component(self, requester, approver):
        component = ComponentFactory()
        body = self._ask(requester, component, amount="99.00").json()
        assert body["payload"]["expected_current_uid"] is None and body["before"] == {}
        assert approver.post(detail(body, "approve/"), {}, format="json").status_code == 200
        assert Price.objects.get(component=component, kind="LIST", effective_to__isnull=True).amount == Decimal("99.00")

    @pytest.mark.parametrize("overrides", [{"amount": "-1"}, {"kind": "PURCHASE"}, {"component_uid": "not-a-uuid"}])
    def test_invalid_price_is_refused_on_submit(self, requester, overrides):
        price = PriceFactory()
        payload = {"component_uid": str(price.component.uid), "kind": "LIST", "amount": "5", **overrides}
        response = requester.post(URL, {"kind": "pricing.price", "operation": "CREATE", "payload": payload}, format="json")
        assert response.status_code == 400 and not ChangeRequest.objects.exists()

    def test_approver_needs_pricing_edit(self, requester, auth_client, make_user):
        body = self._ask(requester, PriceFactory().component).json()
        weak = auth_client(make_user(grants={"change_requests": "*", "pricing": ["view"]}, scopes={"change_requests": "all"}))
        assert weak.post(detail(body, "approve/"), {}, format="json").status_code == 403
