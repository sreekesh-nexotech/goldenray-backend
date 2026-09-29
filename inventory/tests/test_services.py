"""Inventory services called directly: every DomainError, the override rule, concurrency, catalog usage, the settings check."""

import threading
import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.db import connection

from catalog.services import components as component_services
from catalog.services.usage import usage_of
from catalog.tests.factories import ComponentFactory
from core.errors import Conflict, DomainError
from inventory.checks import check_receiving_location
from inventory.models import Movement
from inventory.services import locations
from inventory.services.movements import balance_of, record_movement
from inventory.tests.factories import LocationFactory, MovementFactory, OfficeFactory

pytestmark = pytest.mark.django_db


def data(component, location, **overrides):
    return {"component": component, "location": location, "qty": Decimal("1"), "direction": "IN", "reason": "PURCHASE", **overrides}


def error_of(call) -> DomainError:
    with pytest.raises(DomainError) as caught:
        call()
    return caught.value


class TestLocationServices:
    def test_create_requires_code_and_name(self, keeper):
        error = error_of(lambda: locations.create_location(user=keeper, data={}))
        assert error.code == "validation_error" and set(error.errors) == {"code", "name"}

    def test_code_taken(self, keeper, store):
        error = error_of(lambda: locations.create_location(user=keeper, data={"code": "HO-STORE", "name": "Again"}))
        assert isinstance(error, Conflict) and error.code == "location_code_taken"

    def test_office_instances(self, keeper):
        office = OfficeFactory()
        location = locations.create_location(user=keeper, data={"code": "A", "name": "A", "office": office})
        assert location.office == office and locations.live_office(location) == office
        office.soft_delete()
        location.refresh_from_db()
        assert locations.live_office(location) is None
        assert "office" in error_of(lambda: locations.create_location(user=keeper, data={"code": "B", "name": "B", "office": office})).errors

    def test_malformed_office_uid(self, keeper):
        assert error_of(lambda: locations.create_location(user=keeper, data={"code": "C", "name": "C", "office": "garbage"})).errors == {"office": ["Must be a valid UUID."]}

    def test_blank_name_and_bad_code(self, keeper, store):
        error = error_of(lambda: locations.update_location(store, user=keeper, data={"name": "  ", "code": "a b"}))
        assert set(error.errors) == {"name", "code"}

    def test_a_vanished_row_is_404(self, keeper):
        location = LocationFactory()
        location.soft_delete()
        assert error_of(lambda: locations.update_location(location, user=keeper, data={"name": "x"})).status == 404

    def test_find_by_code(self, store):
        assert locations.find_by_code(" ho-store ") == store and locations.find_by_code("") is None

    def test_delete_with_stock(self, keeper, store, component):
        MovementFactory(component=component, location=store, qty="1", direction="OUT", reason="ADJUST", note="lost")
        error = error_of(lambda: locations.delete_location(store, user=keeper))
        assert error.code == "location_has_stock" and error.status == 409  # a negative balance is stock too


class TestMovementServices:
    def test_instances_and_balance(self, keeper, component, store):
        record_movement(user=keeper, data=data(component, store, qty=Decimal("2.5")))
        movement = record_movement(user=keeper, data=data(component, store, qty="1", direction="OUT", reason="RETURN"))
        assert movement.balance_after == Decimal("1.500") and balance_of(component, store) == Decimal("1.500")
        assert balance_of(component, LocationFactory()) == Decimal("0.000")

    @pytest.mark.parametrize("qty", ["NaN", "Infinity", "1000000000", None, "0.0001"])
    def test_bad_quantities(self, keeper, component, store, qty):
        assert "qty" in error_of(lambda: record_movement(user=keeper, data=data(component, store, qty=qty))).errors

    def test_unknown_direction_and_reason(self, keeper, component, store):
        error = error_of(lambda: record_movement(user=keeper, data=data(component, store, direction="UP", reason="GIFT")))
        assert set(error.errors) == {"direction", "reason"}

    def test_bad_reference_uid(self, keeper, component, store):
        error = error_of(lambda: record_movement(user=keeper, data=data(component, store, ref_type="projects.project", ref_uid="nope")))
        assert "ref_uid" in error.errors

    def test_malformed_uids_and_time(self, keeper, component, store):
        assert error_of(lambda: record_movement(user=keeper, data=data("garbage", store))).errors == {"component": ["Must be a valid UUID."]}
        assert error_of(lambda: record_movement(user=keeper, data=data(component, "garbage"))).errors == {"location": ["Must be a valid UUID."]}
        assert error_of(lambda: record_movement(user=keeper, data=data(component, store, at="yesterday"))).errors == {"at": ["A date-time is required."]}

    def test_naive_time(self, keeper, component, store):
        assert "at" in error_of(lambda: record_movement(user=keeper, data=data(component, store, at=datetime(2026, 1, 1)))).errors

    def test_missing_location(self, keeper, component):
        assert error_of(lambda: record_movement(user=keeper, data=data(component, None))).errors == {"location": ["This field is required."]}

    def test_deleted_component_instance(self, keeper, component, store):
        component.soft_delete()
        assert "component" in error_of(lambda: record_movement(user=keeper, data=data(component, store))).errors
        assert record_movement(user=None, data=data(component, store), include_deleted_component=True).qty == 1

    def test_the_override_needs_inventory_edit(self, make_user, component, store):
        viewer = make_user(grants={"inventory": ["view"]})
        adjust = data(component, store, direction="OUT", reason="ADJUST", note="count")
        for user in (None, viewer):
            error = error_of(lambda user=user: record_movement(user=user, data=adjust))
            assert error.code == "insufficient_stock" and error.status == 409
        assert not Movement.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_concurrent_outs_cannot_both_pass_the_balance_check(make_user):
    """Two OUTs of the whole stock at once: the location row lock serialises them, exactly one succeeds."""
    keeper = make_user(grants={"inventory": ["view", "edit"]})
    component, store = ComponentFactory(), LocationFactory()
    MovementFactory(component=component, location=store, qty="5")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait()
            record_movement(user=keeper, data=data(component, store, qty="5", direction="OUT", reason="RETURN"))
            result = "ok"
        except DomainError as exc:
            result = exc.code
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            result = repr(exc)
        finally:
            connection.close()
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["insufficient_stock", "ok"]
    assert balance_of(component, store) == 0


class TestCatalogUsage:
    def test_stock_blocks_deleting_a_component(self, keeper, component, store):
        MovementFactory(component=component, location=store, qty="3")
        section = next(section for section in usage_of(component) if section.name == "inventory.stock")
        assert section.count == 1 and section.references[0]["label"] == "HO-STORE: 3 in stock" and section.references[0]["status"] == "IN_STOCK"
        error = error_of(lambda: component_services.delete_component(component, user=keeper))
        assert error.code == "component_in_use"

    def test_zero_stock_and_a_disabled_ledger_do_not_block(self, stock_off, keeper, store):
        component = ComponentFactory()
        MovementFactory(component=component, location=store, qty="3")
        assert next(section for section in usage_of(component) if section.name == "inventory.stock").count == 0
        component_services.delete_component(component, user=keeper)


class TestSettingsCheck:
    @pytest.mark.parametrize("value", ["", "  ", "HO-STORE", None])
    def test_valid(self, settings, value):
        settings.INVENTORY_RECEIVING_LOCATION = value
        assert check_receiving_location() == []

    @pytest.mark.parametrize("value", ["has space", "x" * 31, 5])
    def test_invalid(self, settings, value):
        settings.INVENTORY_RECEIVING_LOCATION = value
        [error] = check_receiving_location()
        assert error.id == "inventory.E001"

    def test_manage_py_check_runs_it(self, settings):
        settings.INVENTORY_RECEIVING_LOCATION = "bad code"
        with pytest.raises(Exception, match="inventory.E001"):
            call_command("check")


def test_unknown_location_uid(keeper, component):
    assert error_of(lambda: record_movement(user=keeper, data=data(component, uuid.uuid4()))).errors == {"location": ["Unknown location."]}
