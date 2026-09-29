"""reference.services.legacy_import — counts, idempotency, updates, violations, pincode grouping, audit + cache."""

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from audit.models import AuditLog
from core.models import LegacyMap
from reference.models import Appliance, DeviceType, EvCar, KsebTariff, Pincode, PincodeOffice, Wattage
from reference.services import legacy_import
from reference.tests.factories import DeviceTypeFactory

pytestmark = pytest.mark.django_db
RECORDED = json.loads((Path(__file__).resolve().parent / "legacy" / "backend_reference.json").read_text())
ROWS = RECORDED["rows"]


def test_flat_lists_are_copied_with_legacy_order_and_timestamps():
    result = legacy_import.import_device_types(ROWS["device_types"])
    assert result == {"created": 18, "updated": 0, "skipped": 0, "violations": []}
    iron = DeviceType.objects.get(name="Iron")
    source = next(row for row in ROWS["device_types"] if row["name"] == "Iron")
    assert iron.sort_order == source["id"] and iron.k_value == source["k_value"] and iron.created_at.isoformat() == source["created_at"]
    tariffs = legacy_import.import_tariffs(ROWS["kseb_tariffs"])
    assert tariffs["created"] == 5
    top = KsebTariff.objects.get(slab_to_units__isnull=True)
    assert top.rate_per_unit == Decimal("9.2000") and top.phase is None and top.effective_from is None
    legacy_import.import_ev_cars(ROWS["ev_cars"])
    nexon = EvCar.objects.get(model="Tata Nexon EV")
    assert nexon.ex_showroom_price == Decimal("1249000.00") and nexon.battery_capacity == 30.2
    entry = AuditLog.objects.get(action="reference.legacy_imported", object_type="reference.devicetype")
    assert entry.after["created"] == 18 and entry.after["checksum"] == legacy_import.checksum(ROWS["device_types"]) and entry.actor_kind == "SYSTEM"


def test_rerun_updates_only_what_changed_and_never_duplicates():
    legacy_import.import_wattages(ROWS["wattages"])
    rows = copy.deepcopy(ROWS["wattages"])
    rows[0]["show_in_ui"] = False
    result = legacy_import.import_wattages(rows)
    assert result["created"] == 0 and result["updated"] == 1 and result["skipped"] == len(rows) - 1
    assert Wattage.all_objects.count() == len(rows) and Wattage.objects.get(value=rows[0]["value"]).version == 2


def test_rows_deleted_in_the_platform_stay_deleted():
    legacy_import.import_room_sizes(ROWS["room_size"])
    from reference.models import RoomSize

    RoomSize.objects.first().soft_delete()
    result = legacy_import.import_room_sizes(ROWS["room_size"])
    assert result["skipped"] == len(ROWS["room_size"]) and RoomSize.objects.count() == len(ROWS["room_size"]) - 1


def test_violations_are_reported_and_skipped():
    DeviceTypeFactory(name="IRON")  # created in the platform before the import
    rows = copy.deepcopy(ROWS["device_types"][:3])
    rows[1]["name"] = " "
    result = legacy_import.import_device_types([*rows, next(row for row in ROWS["device_types"] if row["name"] == "Iron")])
    fields = [violation["field"] for violation in result["violations"]]
    assert fields == ["name", "row"] and result["created"] == 2
    bad_rate = legacy_import.import_tariffs([{"id": 90, "min_units": 0, "max_units": None, "rate": "n/a"}])
    assert bad_rate["violations"][0]["field"] == "rate"
    negative = legacy_import.import_ev_cars([dict(ROWS["ev_cars"][0], id=91, model="Broken", ex_showroom_price=-1)])
    assert negative["violations"][0]["field"] == "row" and not EvCar.objects.filter(model="Broken").exists()
    nameless = legacy_import.import_ev_scooters([dict(ROWS["ev_scooters"][0], id=92, model="")])
    assert nameless["violations"][0]["field"] == "model"


def test_pincodes_group_offices_and_take_the_first_offices_district():
    rows = [row for row in ROWS["pincodes"] if row["pincode"] == "686102"]
    assert len({row["district"] for row in rows}) > 1
    result = legacy_import.import_pincodes(rows)
    assert result["created"] == len(rows) and result["violations"] == []
    pincode = Pincode.objects.get(pincode="686102")
    first = min(rows, key=lambda row: row["id"])
    assert pincode.district == first["district"] and pincode.offices.count() == len(rows)
    assert LegacyMap.objects.filter(source_table="pincodes").count() == len(rows)
    again = legacy_import.import_pincodes(rows)
    assert again["skipped"] == len(rows) and Pincode.objects.count() == 1 and PincodeOffice.objects.count() == len(rows)


def test_pincode_district_follows_an_updated_first_office():
    rows = copy.deepcopy([row for row in ROWS["pincodes"] if row["pincode"] == "686102"])
    legacy_import.import_pincodes(rows)
    first = min(rows, key=lambda row: row["id"])
    first["district"] = "ERNAKULAM"
    result = legacy_import.import_pincodes(rows)
    assert result["updated"] == 1 and Pincode.objects.get(pincode="686102").district == "ERNAKULAM"


def test_pincode_violations():
    result = legacy_import.import_pincodes([{"id": 1, "pincode": "12345", "office_name": "x"}, {"id": 2, "pincode": "", "office_name": "y"}])
    assert [violation["field"] for violation in result["violations"]] == ["pincode", "pincode"] and not Pincode.objects.exists()


def test_all_sampled_pincodes_import_cleanly():
    result = legacy_import.import_pincodes(ROWS["pincodes"])
    assert result["created"] == len(ROWS["pincodes"]) and result["violations"] == []
    assert Pincode.objects.count() == len({row["pincode"] for row in ROWS["pincodes"]})


def test_appliances_from_the_flarize_master():
    result = legacy_import.import_appliances(RECORDED["appliances"])
    assert result["created"] == len(RECORDED["appliances"]) and result["violations"] == []
    pump = Appliance.objects.get(code="water_pump")
    assert pump.is_optional is True and pump.default_hours == Decimal("1") and pump.sort_order == 8
    assert LegacyMap.objects.filter(source_system="FLARIZE", source_table="quotation_content.appliances").count() == len(RECORDED["appliances"])
    again = legacy_import.import_appliances(RECORDED["appliances"])
    assert again["skipped"] == len(RECORDED["appliances"])
    bad = legacy_import.import_appliances([{"id": "Bad Id", "name": {"en": "x"}, "watts": 1, "defaultHours": 1}])
    assert bad["violations"][0]["field"] == "id"
