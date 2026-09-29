"""reference.services.lookups — the documented reads other contexts use (calculators, leads, installations)."""

import datetime as dt
from decimal import Decimal

import pytest

from reference.services import lookups
from reference.tests import factories

pytestmark = pytest.mark.django_db


def test_pincode_lookups():
    pincode = factories.PincodeFactory(pincode="686102", district="KOTTAYAM")
    factories.PincodeOfficeFactory(pincode=pincode, district="KOTTAYAM", sort_order=1)
    factories.PincodeOfficeFactory(pincode=pincode, district="ALAPPUZHA", sort_order=2)
    factories.PincodeFactory(pincode="688011", district="ALAPPUZHA")
    factories.PincodeFactory(pincode="688012", district="ALAPPUZHA", is_active=False)
    assert lookups.find_pincode(" 686102 ").pincode == "686102"
    assert lookups.find_pincode("68610") is None and lookups.find_pincode("abcdef") is None and lookups.find_pincode("688012") is None
    assert lookups.district_of("686102") == "KOTTAYAM" and lookups.district_of("999999") is None
    assert lookups.pincodes_in_district("ALAPPUZHA") == ["686102", "688011"]
    assert lookups.pincodes_in_district("") == []


def test_current_tariffs_and_slabs():
    factories.KsebTariffFactory(slab_from_units=0, slab_to_units=300, rate_per_unit=Decimal("6.75"))
    factories.KsebTariffFactory(slab_from_units=301, slab_to_units=None, rate_per_unit=Decimal("7.60"))
    factories.KsebTariffFactory(slab_from_units=0, slab_to_units=None, rate_per_unit=Decimal("8.00"), phase="3P", effective_from=dt.date(2026, 1, 1))
    factories.KsebTariffFactory(slab_from_units=0, slab_to_units=None, rate_per_unit=Decimal("9.00"), phase="3P", effective_from=dt.date(2027, 1, 1))
    factories.KsebTariffFactory(slab_from_units=1, slab_to_units=None, rate_per_unit=Decimal("1.00"), is_active=False)
    on = dt.date(2026, 9, 28)
    assert [row.rate_per_unit for row in lookups.current_tariffs(on=on)] == [Decimal("6.75"), Decimal("7.60"), Decimal("8.00")]
    assert [row.rate_per_unit for row in lookups.current_tariffs(on=on, phase="3P")] == [Decimal("8.00")]
    assert [row.rate_per_unit for row in lookups.current_tariffs(on=on, phase="1P")] == [Decimal("6.75"), Decimal("7.60")]
    assert lookups.current_tariffs(on=dt.date(2027, 6, 1), phase="3P")[0].rate_per_unit == Decimal("9.00")
    assert lookups.slab_for_units(450, on=on).rate_per_unit == Decimal("7.60")
    assert lookups.slab_for_units(120, on=on).rate_per_unit == Decimal("6.75")
    assert lookups.slab_for_units(10, on=on, phase="3P").rate_per_unit == Decimal("8.00")


def test_slab_for_units_without_tariffs():
    assert lookups.slab_for_units(100) is None


def test_device_and_vehicle_lookups():
    factories.DeviceTypeFactory(name="AC 1 ton")
    factories.EvCarFactory(model="Tata Nexon EV")
    factories.EvScooterFactory(model="Ather 450X")
    assert lookups.device_type_by_name(" ac 1 TON ").name == "AC 1 ton" and lookups.device_type_by_name("Toaster") is None
    assert lookups.vehicle_by_model("Tata Nexon EV").__class__.__name__ == "EvCar"
    assert lookups.vehicle_by_model("Ather 450X").__class__.__name__ == "EvScooter"
    assert lookups.vehicle_by_model("Unknown") is None
