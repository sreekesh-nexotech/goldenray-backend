"""TEMPORARY reviewer fuzz (not committed): random EMI requests against the legacy UAT server and the platform."""

import json
import os
import random
import urllib.error
import urllib.request

import pytest

from calculators.tests.parity.support import compare
from emi.services import legacy_import
from emi.tests.parity import translate
from emi.tests.test_parity import _rows

pytestmark = pytest.mark.django_db
LEGACY = os.environ.get("FUZZ_LEGACY", "http://127.0.0.1:18012")
N = int(os.environ.get("FUZZ_N", "1500"))
SEED = int(os.environ.get("FUZZ_SEED", "20260929"))

NUMS = ["0", "1", "2", "3", "5", "8", "10", "-1", "2.5", "3.0", "3.0000001", "5.00001", "1e400", "1e30", "0.001", "99999999999999999999999", "7.9", "18", "25", "33.333333", "90", "95", "150000", "210000", "250000.55", "480000", "900000", "1000000"]
TEXTS = ['"5"', '"8.00"', '"abc"', '"nan"', '"inf"', '"-inf"', '""', '" 3 "', '"\\ud800"', '"3\\u0000"', '"\\udc00x"', '"١٢"', '"5_0"', '"0x10"', '"1e3"', '" TRUE "', '"yes"', '"no"', '"off"']
OTHER = ["true", "false", "null", "[]", "[5]", "{}", '{"a": 1}']


def value(rng):
    pool = rng.choice([NUMS, NUMS, TEXTS, OTHER])
    return rng.choice(pool)


def calc_body(rng):
    fields = []
    if rng.random() < 0.6:
        fields.append(("size_id", rng.choice(["1", "2", "3", "4", "99", "0", '"2"', "3.7", "true", '"two"', '"\\ud800"'])))
    for key in ("capacity_kw", "power_capacity", "tenure_years", "interest_rate", "system_cost", "price", "down_payment_percent", "apply_down_payment", "apply_subsidy"):
        if rng.random() < 0.35:
            fields.append((key, value(rng)))
    if rng.random() < 0.05:
        fields.append(('"x\\ud800"', "1"))
    return "{" + ", ".join((k if k.startswith('"') else json.dumps(k)) + ": " + v for k, v in fields) + "}"


def quote_body(rng):
    packages = []
    for _ in range(rng.choice([0, 1, 1, 2, 3, 6, 7])):
        key = rng.choice(['"eco"', '"premium"', '"p\\ud800"', '"' + "k" * 40 + '"', '"a"', '"b"', '"c"'])
        if rng.random() < 0.1:
            packages.append((key, value(rng)))
            continue
        inner = []
        if rng.random() < 0.9:
            inner.append('"system_cost": ' + rng.choice(NUMS + TEXTS[:6] + OTHER))
        if rng.random() < 0.5:
            inner.append('"subsidy": ' + rng.choice(NUMS + TEXTS[:6] + OTHER))
        packages.append((key, "{" + ", ".join(inner) + "}"))
    fields = []
    if rng.random() < 0.9:
        fields.append('"packages": {' + ", ".join(f"{k}: {v}" for k, v in packages) + "}")
    if rng.random() < 0.85:
        fields.append('"capacity_kw": ' + rng.choice(["3", "5", "0", "-1", '"3"', "8.5", "1e400", '"abc"', "true", "null", "[3]", '"\\ud800"', "2.99"]))
    if rng.random() < 0.5:
        fields.append('"tenure_years": ' + rng.choice(["1", "5", "10", "11", "0", '"7"', "2.5", '"x"', "null", "true", "1e400"]))
    return "{" + ", ".join(fields) + "}"


def dumps_ascii(value):
    from decimal import Decimal

    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(key)}: {dumps_ascii(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(dumps_ascii(item) for item in value) + "]"
    if isinstance(value, Decimal):
        return str(value)
    return json.dumps(value)


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


def test_fuzz_emi(api_client):
    rows = _rows(os.environ.get("FUZZ_VARIANT", "uat"))
    legacy_import.import_all(
        banks=rows["emi_bank"], interest_rules=rows["emi_interest_rate_rule"], subsidy_rules=rows["emi_subsidy_rule"], settings=rows["emi_calculator_settings"], system_sizes=rows["emi_system_size"]
    )
    maps = translate.uid_maps()
    rng = random.Random(SEED)
    differences = []
    for index in range(N):
        kind = "calculate" if index % 2 == 0 else "quotation"
        text = calc_body(rng) if kind == "calculate" else quote_body(rng)
        path = "/api/emi-calculator/" if kind == "calculate" else "/api/emi-calculator/quotation/"
        status, legacy_body = legacy_post(path, text)
        case = {"name": text, "status": status, "response": legacy_body, "error_class": None}
        if kind == "calculate":
            try:
                platform_text = dumps_ascii(translate.request(translate.loads(text), maps))
            except Exception as exc:  # noqa: BLE001
                differences.append(f"translate failed {text!r}: {exc!r}")
                continue
            target = "/api/public/v1/calculators/emi/"
        else:
            platform_text, target = text, "/api/public/v1/calculators/emi/quotation/"
        response = api_client.post(target, data=platform_text.encode("utf-8", "surrogatepass"), content_type="application/json")
        got = response.json() if response.content else None
        difference = compare(case, response.status_code, got, canonical=lambda legacy: translate.response(legacy, maps), message=translate.message)
        if difference:
            differences.append(f"{kind} {text}: {difference}")
    print(f"{N} requests, {len(differences)} differences")
    assert not differences, "\n".join(differences[:40])
