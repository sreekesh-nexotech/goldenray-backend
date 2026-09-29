"""Parity: after importing the legacy rows, the public reference payloads carry every legacy field with the same value.

``legacy/backend_reference.json`` was recorded by ``capture_backend.py`` from the UAT main backend (``/api/<list>/``
on :18012, read-only) together with the source rows. The legacy integer ``id`` becomes the row's ``uid`` (resolved
through ``core_legacy_map``); the tariff columns carry the PLAN names (:data:`RENAMED`). Rows come back in legacy-id
order (the legacy endpoints had no ORDER BY; the website sorts device types itself).
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from core.models import LegacyMap
from reference.services import legacy_import

pytestmark = pytest.mark.django_db
RECORDED = json.loads((Path(__file__).resolve().parent / "legacy" / "backend_reference.json").read_text())
TABLES = {"tariffs": "kseb_tariffs", "device-types": "device_types", "wattages": "wattages", "room-sizes": "room_size", "ev-cars": "ev_cars", "ev-scooters": "ev_scooters"}
RENAMED = {"tariffs": {"min_units": "slab_from_units", "max_units": "slab_to_units", "rate": "rate_per_unit"}}
IMPORTERS = {
    "kseb_tariffs": legacy_import.import_tariffs,
    "device_types": legacy_import.import_device_types,
    "wattages": legacy_import.import_wattages,
    "room_size": legacy_import.import_room_sizes,
    "ev_cars": legacy_import.import_ev_cars,
    "ev_scooters": legacy_import.import_ev_scooters,
    "pincodes": legacy_import.import_pincodes,
}


@pytest.fixture
def imported():
    for table, importer in IMPORTERS.items():
        result = importer(RECORDED["rows"][table])
        assert result["violations"] == [] and result["created"] == len(RECORDED["rows"][table]), table
    legacy_import.import_appliances(RECORDED["appliances"])


def same(legacy, new) -> bool:
    if legacy is None or new is None:
        return legacy is new
    if isinstance(new, str) and isinstance(legacy, (int, float, str)) and not isinstance(legacy, bool):
        try:
            return Decimal(new) == Decimal(str(legacy))
        except ArithmeticError:
            return new == legacy
    return new == legacy


@pytest.mark.parametrize("key", list(TABLES))
def test_list_payload_matches_the_legacy_endpoint(api_client, imported, key):
    table = TABLES[key]
    body = api_client.get(f"/api/public/v1/reference/{key}/", {"page_size": 200}).json()
    legacy_rows = RECORDED["responses"][key]
    assert body["count"] == len(legacy_rows) and body["next"] is None
    legacy_by_target = dict(LegacyMap.objects.filter(source_system="BACKEND", source_table=table).values_list("target_id", "source_id"))
    model = {"kseb_tariffs": "KsebTariff", "device_types": "DeviceType", "wattages": "Wattage", "room_size": "RoomSize", "ev_cars": "EvCar", "ev_scooters": "EvScooter"}[table]
    from reference import models

    uid_to_legacy = {str(uid): int(legacy_by_target[pk]) for pk, uid in getattr(models, model).objects.values_list("pk", "uid")}
    new_by_legacy = {uid_to_legacy[row["uid"]]: row for row in body["results"]}
    assert [uid_to_legacy[row["uid"]] for row in body["results"]] == sorted(new_by_legacy)
    renamed = RENAMED.get(key, {})
    for legacy in legacy_rows:
        new = new_by_legacy[legacy["id"]]
        for field, value in legacy.items():
            if field == "id":
                continue
            assert same(value, new[renamed.get(field, field)]), (key, legacy["id"], field, value, new)


def test_pincode_lookup_matches_the_legacy_rows(api_client, imported):
    legacy_rows = RECORDED["responses"]["pincodes"]
    codes = sorted({row["pincode"] for row in legacy_rows})
    assert len(codes) >= 8
    for code in codes:
        offices = sorted((row for row in legacy_rows if row["pincode"] == code), key=lambda row: row["id"])
        body = api_client.get(f"/api/public/v1/reference/pincodes/{code}/").json()
        assert body["pincode"] == code and body["district"] == offices[0]["district"] and body["state"] == offices[0]["state"]
        assert body["offices"] == [{field: row[field] for field in ("office_name", "district", "state", "region", "division")} for row in offices]


def test_appliances_match_the_flarize_master(api_client, imported):
    body = api_client.get("/api/public/v1/reference/appliances/", {"page_size": 200}).json()
    master = RECORDED["appliances"]
    assert [row["code"] for row in body["results"]] == [item["id"] for item in master]
    for row, item in zip(body["results"], master, strict=True):
        assert row["name"] == item["name"]["en"] and row["name_ml"] == item["name"]["ml"] and row["icon"] == item["icon"]
        assert row["watts"] == item["watts"] and Decimal(row["default_hours"]) == Decimal(str(item["defaultHours"])) and row["is_optional"] == bool(item.get("optional"))
