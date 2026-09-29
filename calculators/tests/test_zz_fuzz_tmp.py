"""TEMPORARY reviewer fuzz (not committed): random calculator requests against a legacy server and the platform."""

import json
import os
import random
import urllib.error
import urllib.request

import pytest

from calculators.tests.parity.support import ENDPOINTS, battery_price_provider, compare, import_legacy_rows, load_rows
from catalog.services import pricing_hooks

pytestmark = pytest.mark.django_db
LEGACY = os.environ.get("FUZZ_LEGACY", "http://127.0.0.1:18012")
N = int(os.environ.get("FUZZ_N", "1500"))
SEED = int(os.environ.get("FUZZ_SEED", "99"))
VARIANT = os.environ.get("FUZZ_VARIANT", "uat")
LEGACY_PATHS = {"basic": "/api/calculate-solar/", "basic_v2": "/api/calculate-solar-new/", "advanced": "/api/calculate-solar-advanced/"}

DEVICES = ["AC 1 ton", "ac 1 TON", "Washing Machine", "TV", "tv", "Fan", "FAN ", "Heater", "Light", "LIGHT", "light", "Lights", "Iron", "AC 2 ton", "Toaster", "Toaſter", "Water Pump", "Router", "Induction Cooktop", "", "x"]
EVS = ["Tata Nexon EV", "tata nexon ev", "Ola S1 Pro", "Ather 450X", "TVS iQube", "Prototype EV", "Fleet Van EV", "Simple One", "", "Unknown"]
NUMS = ["0", "1", "2", "3", "4.5", "10", "24", "-1", "0.5", "100", "1000", "1e3", "250.75"]
NUMTEXT = ['"2"', '"3.5"', '"abc"', '""', '"1e2"', '" 4 "']
ODD = ["null", "true", "false", "[]", "{}", "[2]"]


def pick(rng, pools):
    return rng.choice(rng.choice(pools))


def device(rng, names=DEVICES):
    fields = []
    if rng.random() < 0.95:
        fields.append('"device_type": ' + (json.dumps(rng.choice(names)) if rng.random() < 0.9 else pick(rng, [ODD, NUMS])))
    if rng.random() < 0.9:
        fields.append('"daily_usage": ' + pick(rng, [NUMS, NUMS, NUMTEXT, ODD[:1]]))
    if rng.random() < 0.6:
        fields.append('"no_of_units": ' + pick(rng, [["1", "2", "3", "5", "0", "-2"], ['"2"', "2.7", '"x"']]))
    return "{" + ", ".join(fields) + "}"


def ev(rng):
    fields = []
    if rng.random() < 0.95:
        fields.append('"model": ' + (json.dumps(rng.choice(EVS)) if rng.random() < 0.9 else pick(rng, [ODD, NUMS])))
    if rng.random() < 0.9:
        fields.append('"daily_avg_km": ' + pick(rng, [NUMS, NUMS, NUMTEXT]))
    if rng.random() < 0.5:
        fields.append('"no_of_vehicles": ' + rng.choice(["1", "2", "0", '"3"', "1.5"]))
    return "{" + ", ".join(fields) + "}"


def advanced_body(rng):
    specs = []
    specs.append('"grid_type": ' + rng.choice(['"On Grid"', '"Hybrid"', '"Hybrid"', '"Off Grid"', '"on grid"']))
    if rng.random() < 0.8:
        specs.append('"home_type": ' + rng.choice(['"New Home"', '"Existing Home"', '"new home"', "null"]))
    if rng.random() < 0.6:
        specs.append('"estimated_base_load": ' + pick(rng, [NUMS, NUMS, NUMTEXT, ["0", "600", "1200", "5000"]]))
    if rng.random() < 0.6:
        specs.append('"average_bill": ' + pick(rng, [["500", "1500", "2000", "3500", "4050", "6000", "9000", "20000", "60000"], NUMTEXT, NUMS]))
    if rng.random() < 0.5:
        specs.append('"bill_frequency": ' + rng.choice(['"Monthly"', '"BI-Monthly"', '"monthly"', "null", '""']))
    usage = []
    if rng.random() < 0.8:
        usage.append('"usage_electronic_devices": [' + ", ".join(device(rng) for _ in range(rng.choice([0, 1, 2, 3, 5, 8]))) + "]")
    if rng.random() < 0.5:
        usage.append('"electric_vehicles": [' + ", ".join(ev(rng) for _ in range(rng.choice([0, 1, 2, 3]))) + "]")
    preference = []
    if rng.random() < 0.7:
        preference.append('"backup_hours": ' + pick(rng, [["0", "1", "2", "3", "4", "6", "8", "12", "24", "2.5", "0.1"], NUMTEXT, ["null"]]))
    if rng.random() < 0.6:
        preference.append('"preference_electronic_devices": [' + ", ".join(device(rng) for _ in range(rng.choice([0, 1, 2, 4]))) + "]")
    parts = ['"Specifications": {' + ", ".join(specs) + "}"]
    if rng.random() < 0.95:
        parts.append('"usageDetails": {' + ", ".join(usage) + "}")
    if rng.random() < 0.8:
        parts.append('"preferenceDetails": {' + ", ".join(preference) + "}")
    return "{" + ", ".join(parts) + "}"


PINCODES = ['"682001"', '"695001"', "682001", '"999999"', '" 682001"', "682001.0", '"68200"', "null", '""']
TYPES = ['"Residential"', '"residential"', '"RESIDENTIAL"', '"Commercial"', '"commercial"', '"Industrial"', '""', "null", "5", '"Reſidential"']


def basic_body(rng):
    bill = rng.choice(
        [
            str(rng.randint(0, 45000)),
            f"{rng.uniform(0, 45000):.2f}",
            str(rng.choice([6000, 6001, 8000, 8001, 10000, 15500, 15501, 20000, 24000, 30000, 30001, 40000, 40001, 197, 198])),
            rng.choice(NUMTEXT + ODD + ['"6000"', '"8000.5"']),
        ]
    )
    fields = ['"monthly_bill": ' + bill]
    if rng.random() < 0.95:
        fields.append('"pincode": ' + rng.choice(PINCODES))
    if rng.random() < 0.95:
        fields.append('"property_type": ' + rng.choice(TYPES))
    rng.shuffle(fields)
    return "{" + ", ".join(fields) + "}"


def legacy_post(path, text):
    request = urllib.request.Request(LEGACY + path, data=text.encode("utf-8", "surrogatepass"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    try:
        parsed = json.loads(body)
    except Exception:  # noqa: BLE001
        parsed = None
    return status, parsed


def test_fuzz_calculators(api_client):
    previous = pricing_hooks.current_provider()
    pricing_hooks.register(battery_price_provider(import_legacy_rows(load_rows(VARIANT))))
    try:
        rng = random.Random(SEED)
        differences = []
        for index in range(N):
            key = ("basic", "basic_v2", "advanced", "advanced")[index % 4]
            text = advanced_body(rng) if key == "advanced" else basic_body(rng)
            status, legacy_body = legacy_post(LEGACY_PATHS[key], text)
            case = {"name": text, "status": status, "response": legacy_body, "error_class": None}
            response = api_client.post(ENDPOINTS[key], data=text.encode("utf-8"), content_type="application/json")
            difference = compare(case, response.status_code, response.json() if response.content else None)
            if difference:
                differences.append(f"{key} {text}: {difference}")
        print(f"{N} requests, {len(differences)} differences")
        assert not differences, "\n".join(differences[:30])
    finally:
        pricing_hooks.register(previous)
