"""calculators services: sizing writes (every DomainError), the battery source and the cached table snapshot."""

import datetime as dt
from decimal import Decimal

import pytest

from calculators.models import CapacitySize
from calculators.services import batteries, data, sizing
from calculators.tests.factories import BillRangeSizeFactory, CapacitySizeFactory
from catalog.services import pricing_hooks
from core.errors import Conflict, DomainError, StaleVersion
from reference.tests.factories import DeviceTypeFactory, EvCarFactory, KsebTariffFactory, PincodeFactory

pytestmark = pytest.mark.django_db


class TestSizingWrites:
    def test_create_and_update(self):
        row = sizing.create_row(
            sizing.CAPACITY_SIZES, user=None, data={"power_capacity_kw": Decimal("3"), "installation_days": 3, "total_cost": Decimal("1"), "total_subsidy": Decimal("0"), "area_required_sqft": 1}
        )
        assert row.version == 1 and CapacitySize.objects.count() == 1
        assert sizing.update_row(sizing.CAPACITY_SIZES, row, user=None, data={"total_cost": Decimal("1")}).version == 1  # nothing changed
        assert sizing.update_row(sizing.CAPACITY_SIZES, row, user=None, data={"total_cost": Decimal("2")}).version == 2

    def test_duplicate_is_conflict(self):
        CapacitySizeFactory(power_capacity_kw=Decimal("3"))
        with pytest.raises(Conflict) as excinfo:
            sizing.create_row(
                sizing.CAPACITY_SIZES, user=None, data={"power_capacity_kw": Decimal("3"), "installation_days": 3, "total_cost": Decimal("1"), "total_subsidy": Decimal("0"), "area_required_sqft": 1}
            )
        assert excinfo.value.code == "capacity_size_exists"

    def test_update_into_a_duplicate_is_conflict(self):
        BillRangeSizeFactory(bill_range=6000)
        other = BillRangeSizeFactory(bill_range=8000)
        with pytest.raises(Conflict):
            sizing.update_row(sizing.BILL_RANGE_SIZES, other, user=None, data={"bill_range": 6000})

    def test_database_rule_is_validation_error(self):
        with pytest.raises(DomainError) as excinfo:
            sizing.create_row(
                sizing.CAPACITY_SIZES, user=None, data={"power_capacity_kw": Decimal("-1"), "installation_days": 3, "total_cost": Decimal("1"), "total_subsidy": Decimal("0"), "area_required_sqft": 1}
            )
        assert excinfo.value.code == "validation_error"
        row = CapacitySizeFactory()
        with pytest.raises(DomainError) as excinfo:
            sizing.update_row(sizing.CAPACITY_SIZES, row, user=None, data={"total_subsidy": Decimal("-1")})
        assert excinfo.value.code == "validation_error"

    def test_stale_version(self):
        row = CapacitySizeFactory()
        with pytest.raises(StaleVersion):
            sizing.update_row(sizing.CAPACITY_SIZES, row, user=None, data={"is_active": False}, expected_version=5)
        with pytest.raises(StaleVersion):
            sizing.delete_row(sizing.CAPACITY_SIZES, row, user=None, expected_version=5)


class TestBatteries:
    def test_only_published_priced_batteries_with_a_capacity_are_offered(self, legacy_tables):
        offered = batteries.backup_batteries()
        assert sorted(battery.capacity for battery in offered) == [Decimal("4.61"), Decimal("5.00"), Decimal("5.12"), Decimal("5.53")]
        assert {battery.price for battery in offered} == {Decimal("140300.00"), Decimal("145000.00"), Decimal("151400.00")}

    def test_a_battery_without_a_price_is_not_offered(self, legacy_tables):
        pk = next(iter(legacy_tables))
        pricing_hooks.register(lambda components: {component.pk: pricing_hooks.PriceInfo(min_amount=legacy_tables[component.pk]) for component in components if component.pk != pk})
        assert len(batteries.backup_batteries()) == 3

    def test_max_amount_is_used_when_only_a_ceiling_is_known(self):
        assert batteries.price_of(pricing_hooks.PriceInfo(max_amount=Decimal("5"))) == Decimal("5")
        assert batteries.price_of(None) is None

    def test_without_a_price_provider_no_battery_is_offered(self, legacy_tables):
        pricing_hooks.reset()
        assert batteries.backup_batteries() == ()


class TestSnapshot:
    def test_reads_every_table_the_calculators_need(self):
        KsebTariffFactory(slab_from_units=0, slab_to_units=300, sort_order=2)
        KsebTariffFactory(slab_from_units=301, slab_to_units=None, sort_order=1, phase="3P")
        DeviceTypeFactory(name="Fan", watts=60, k_value=1)
        EvCarFactory(model="Tata Nexon EV", energy_consumption=0.152)
        PincodeFactory(pincode="682001")
        CapacitySizeFactory(power_capacity_kw=Decimal("3"))
        BillRangeSizeFactory(interest_rate=Decimal("0.0890"))
        snapshot = data.load_data(dt.date(2026, 9, 29))
        assert [slab.min_units for slab in snapshot.tariffs] == [0]  # the every-phase schedule (no 1P schedule of its own)
        assert snapshot.device_types[0].name == "Fan" and snapshot.ev_cars[0].model == "Tata Nexon EV" and snapshot.ev_scooters == ()
        assert snapshot.pincodes == frozenset({"682001"})
        assert snapshot.bill_range_sizes[0].interest_rate == Decimal("8.90") and snapshot.bill_range_sizes[0].property_type == "Residential"
        assert snapshot.capacity_sizes[0].power_capacity == Decimal("3.000")

    def test_cache_failures_fall_back_to_the_database(self, monkeypatch):
        from django.core.cache import cache

        CapacitySizeFactory(power_capacity_kw=Decimal("3"))

        def broken(*args, **kwargs):
            raise ConnectionError("redis down")

        monkeypatch.setattr(cache, "get", broken)
        monkeypatch.setattr(cache, "set", broken)
        assert data.calculator_data().capacity_sizes[0].power_capacity == Decimal("3.000")
