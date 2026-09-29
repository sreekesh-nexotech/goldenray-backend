#!/usr/bin/env python3
"""Record the legacy website calculators for the parity tests (run by hand; never in CI).

    PGPASSWORD=postgres python calculators/tests/parity/capture_legacy.py \\
        --db legacy_goldenapp --url http://127.0.0.1:18012 --variant uat

    # the enriched private copy (never the shared database): see enrich_private.sql
    PGPASSWORD=postgres python calculators/tests/parity/capture_legacy.py \\
        --db legacy_goldenapp_calculators_emi --url http://127.0.0.1:18161 --variant enriched

Writes ``calculators/tests/parity/<variant>/``:

* ``legacy_rows.json`` — the rows the three calculators read, exported with ``SELECT *`` in a read-only transaction:
  ``kseb_tariffs``, ``device_types``, ``ev_cars``, ``ev_scooters``, ``solar_installations``,
  ``solar_installation_new``, ``batteries`` and the post-office rows (``pincodes``) of every pincode the grid uses;
* ``corpus_basic.json``, ``corpus_basic_v2.json``, ``corpus_advanced.json`` — one case per input of a deterministic
  grid (:func:`basic_cases`, :func:`basic_v2_cases`, :func:`advanced_cases`): the raw JSON request body as sent (``request``), the
  legacy status, the parsed JSON response (``response``, ``null`` for a non-JSON answer) and, for a 500, the exception class the
  debug page named.

The calculators only read the database, so the shared UAT server is safe to call. No personal data is involved.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLES = ("kseb_tariffs", "device_types", "ev_cars", "ev_scooters", "solar_installations", "solar_installation_new", "batteries")
DEVICE_NAMES = [
    "AC 1 ton",
    "AC 1.5 ton",
    "AC 2 ton",
    "Washing Machine",
    "TV",
    "Printer",
    "Air Fryer",
    "Cooker",
    "Refrigerator",
    "Microwave",
    "Toaster",
    "Blender",
    "Dishwasher",
    "Vacuum Cleaner",
    "Electric Kettle",
    "Fan",
    "Heater",
    "Iron",
    "Water Pump",  # the enriched copy's watts-and-k device
    "Router",  # … its device without watts
    "Induction Cooktop",  # … its device with k_value 0 (read as 1.0)
]
EV_MODELS = [
    "Tata Nexon EV",
    "Tata Tigor EV",
    "MG ZS EV",
    "Hyundai Kona Electric",
    "Mahindra XUV400",
    "Tata Tiago EV",
    "MG Comet EV",
    "Tata Punch EV",
    "Hyundai Creta Electric",
    "Mahindra BE 6",
    "Mahindra XEV 9e",
    "MG Windsor EV",
    "Tata Curvv EV",
    "Maruti Suzuki e Vitara",
    "Ola S1 Pro",
    "Ather 450X",
    "TVS iQube",
    "Bajaj Chetak",
    "Hero Vida V1",
    "Simple One",
    "Honda Activa e",
    "Suzuki e-Access",
    "Yulu Wynn",
    "Ampere Magnus Neo",
    "BGauss C12i",
    "Okinawa Okhi-90",
    "Revolt RV400",
    "Ultraviolette Tesseract",
    "Ather Rizta",
    "Prototype EV",  # the enriched copy's car without a consumption
    "Fleet Van EV",  # … its car with k_value 0.8
]
KERALA_PINCODES = ["682001", "695001", "673001", "680001", "688001", "686001", "685501", "670001", "678001", "691001", "679101", "689101", "671121", "673592"]
UNKNOWN_PINCODES = ["999999", "110001", "600001", "682999", "000000"]


def raw(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def body(**fields) -> str:
    return raw(fields)


# ── basic (calculate-solar) ───────────────────────────────────────────────────────────────────────────────────────
def pincode_variants() -> list[tuple[str, str]]:
    """``(label, JSON text of the pincode value)`` — every class of value a client can send."""
    variants = [(f"pincode {code}", raw(code)) for code in KERALA_PINCODES]
    variants += [(f"unknown {code}", raw(code)) for code in UNKNOWN_PINCODES]
    variants += [
        ("int", "682001"),
        ("int unknown", "999999"),
        ("float", "682001.0"),
        ("leading space", raw(" 682001")),
        ("trailing space", raw("682001 ")),
        ("five digits", raw("68200")),
        ("seven digits", raw("6820011")),
        ("letters", raw("abcdef")),
        ("true", "true"),
        ("false", "false"),
        ("zero", "0"),
        ("empty", raw("")),
        ("null", "null"),
        ("list", raw(["682001"])),
        ("object", raw({"code": "682001"})),
        ("nul char", raw("6820\u000001")),
        ("unicode digits", raw("６８２００１")),
    ]
    return variants


def property_type_variants() -> list[tuple[str, str]]:
    values = ["Residential", "residential", "RESIDENTIAL", "Commercial", "commercial", "COMMERCIAL", "Industrial", "Resi_dential", "Residential%", "Reſidential", " Residential", "Residential "]
    variants = [(repr(value), raw(value)) for value in values]
    variants += [("empty", raw("")), ("null", "null"), ("number", "5"), ("true", "true"), ("list", raw(["Residential"])), ("object", raw({"type": "Residential"})), ("nul", raw("Resi\u0000dential"))]
    return variants


def _fields(bill: str, pincode: str = '"682001"', property_type: str = '"Residential"', *, omit: tuple[str, ...] = ()) -> str:
    parts = []
    for name, value in (("monthly_bill", bill), ("pincode", pincode), ("property_type", property_type)):
        if name not in omit:
            parts.append(f'"{name}": {value}')
    return "{" + ", ".join(parts) + "}"


def common_invalid_bodies() -> list[tuple[str, str]]:
    return [
        ("empty object", "{}"),
        ("list body", "[1, 2]"),
        ("string body", raw("monthly_bill=3000")),
        ("number body", "3000"),
        ("null body", "null"),
        ("missing bill", _fields("3000", omit=("monthly_bill",))),
        ("missing pincode", _fields("3000", omit=("pincode",))),
        ("missing type", _fields("3000", omit=("property_type",))),
    ]


def basic_bills() -> list[str]:
    """Bills around every slab edge (fixed charges ₹197.08; slabs 0-300 @6.75, -350 @7.60, -400 @7.95, -500 @8.25, 501+ @9.20),
    a sweep across 1-20 kW, and invalid values."""
    edges = [197.08, 2222.08, 2602.08, 2999.58, 3824.58]
    values = []
    for edge in edges:
        values += [round(edge - 0.01, 2), edge, round(edge + 0.01, 2)]
    values += list(range(200, 20001, 200))
    values += [1, 50, 196, 197, 198, 1001, 4999.99, 25000, 30000, 45000, 60000, 100000, 1000000, 0.01, 0.5, 12345.678]
    texts = [raw(value) for value in values]
    texts += ["1e308", "1e400", "-1e400", "-1", "-0.5", "0", "0.0", "true", "false", raw("3000"), raw("abc"), raw(""), "null", "[3000]", '{"amount": 3000}']
    return texts


def basic_cases() -> list[dict]:
    cases = [{"name": f"bill {text}", "request": _fields(text)} for text in basic_bills()]
    cases += [{"name": f"pincode {label}", "request": _fields("3500", pincode=value)} for label, value in pincode_variants()]
    cases += [{"name": f"type {label}", "request": _fields("4200", property_type=value)} for label, value in property_type_variants()]
    cases += [{"name": label, "request": text} for label, text in common_invalid_bodies()]
    cases += [{"name": f"commercial bill {bill}", "request": _fields(raw(bill), pincode=raw("695001"), property_type=raw("Commercial"))} for bill in (1500, 3000, 6000, 9000)]
    return cases


# ── basic-v2 (calculate-solar-new) ────────────────────────────────────────────────────────────────────────────────
def basic_v2_bills() -> list[str]:
    edges = [6000, 8000, 10000, 15500, 20000, 24000, 30000, 40000]
    values = [1, 100, 2500, 12000, 17777, 22000, 27500, 35000]
    for edge in edges:
        values += [edge - 1, edge, edge + 1]
    texts = [raw(value) for value in values]
    texts += [
        "45000",
        "100000",
        "6000.9",
        "8000.4",
        "40000.99",
        "40001.5",
        raw("5000"),
        raw(" 7000 "),
        raw("5_000"),
        raw("5000.5"),
        raw("+9000"),
        raw("٣٠٠٠"),
        raw("abc"),
        raw(""),
        "true",
        "false",
        "0",
        "-1",
        "-6000",
        "null",
        "[5000]",
        '{"amount": 5000}',
        "1e400",
        "1e30",
    ]
    return texts


def basic_v2_cases() -> list[dict]:
    cases = []
    for text in basic_v2_bills():
        cases.append({"name": f"residential bill {text}", "request": _fields(text)})
    for bill in (5000, 6001, 9000, 12000, 16000, 21000, 25000, 31000, 40000, 41000):
        cases.append({"name": f"commercial bill {bill}", "request": _fields(raw(bill), property_type=raw("Commercial"))})
    for bill in range(333, 40001, 777):  # a sweep through every band, both property types
        cases.append({"name": f"sweep residential {bill}", "request": _fields(raw(bill))})
        cases.append({"name": f"sweep commercial {bill}", "request": _fields(raw(bill), pincode=raw("673001"), property_type=raw("commercial"))})
    cases += [{"name": f"pincode {label}", "request": _fields("9000", pincode=value)} for label, value in pincode_variants()]
    cases += [{"name": f"type {label}", "request": _fields("14000", property_type=value)} for label, value in property_type_variants()]
    cases += [{"name": f"type {label} bill 45000", "request": _fields("45000", property_type=value)} for label, value in property_type_variants()[:3]]
    cases += [{"name": label, "request": text} for label, text in common_invalid_bodies()]
    return cases


# ── advanced (calculate-solar-advanced) ───────────────────────────────────────────────────────────────────────────
def advanced_body(specs=None, devices=None, evs=None, preference=None, *, raw_specs: str | None = None, raw_usage: str | None = None, raw_preference: str | None = None) -> str:
    parts = []
    parts.append('"Specifications": ' + (raw_specs if raw_specs is not None else raw(specs if specs is not None else {"home_type": "Existing Home", "grid_type": "On Grid", "average_bill": 3000})))
    if raw_usage is not None:
        parts.append('"usageDetails": ' + raw_usage)
    elif devices is not None or evs is not None:
        usage = {}
        if devices is not None:
            usage["usage_electronic_devices"] = devices
        if evs is not None:
            usage["electric_vehicles"] = evs
        parts.append('"usageDetails": ' + raw(usage))
    if raw_preference is not None:
        parts.append('"preferenceDetails": ' + raw_preference)
    elif preference is not None:
        parts.append('"preferenceDetails": ' + raw(preference))
    return "{" + ", ".join(parts) + "}"


def advanced_cases() -> list[dict]:  # noqa: C901 - a grid, not logic
    cases: list[dict] = []

    def add(name: str, text: str) -> None:
        cases.append({"name": name, "request": text})

    existing = {"home_type": "Existing Home", "grid_type": "On Grid"}
    # base load from the average bill (slab edges: 0-300 @6.75 → 2025, 301-350 @7.60 → 2660, … 501+ @9.20)
    for bill in (0, 100, 1000, 2025, 2026, 2280, 2287.5, 2660, 2700, 3000, 3182.5, 3200, 3700, 4125, 4130, 4609, 4609.2, 5000, 8000, 12000, 20000, 40000, 80000, 250000):
        add(f"existing on-grid bill {bill}", advanced_body({**existing, "average_bill": bill}))
    for text in (raw("3000"), raw("abc"), raw(""), "null", "true", "[3000]", raw("1e400"), raw("nan"), "-500"):
        add(f"existing average_bill {text}", advanced_body(raw_specs='{"home_type": "Existing Home", "grid_type": "On Grid", "average_bill": ' + text + "}"))
    # new home: estimated base load in units
    for load in (0, 50, 120, 300, 301, 450, 600, 900, 1500, 3000, 6000):
        add(f"new home load {load}", advanced_body({"home_type": "New Home", "grid_type": "On Grid", "estimated_base_load": load}))
    for text in (raw("450"), raw("abc"), "null", "[1]", raw("1e400"), raw("-100")):
        add(f"new home load {text}", advanced_body(raw_specs='{"home_type": "New Home", "grid_type": "On Grid", "estimated_base_load": ' + text + "}"))
    # grid types, home types, bill frequency
    for grid in ("On Grid", "Hybrid", "Off Grid", "on grid", "", None, 5):
        add(f"grid {grid!r}", advanced_body({"home_type": "Existing Home", "grid_type": grid, "average_bill": 4000}))
    for home in ("New Home", "Existing Home", "new home", None, "Villa"):
        add(f"home {home!r}", advanced_body({"home_type": home, "grid_type": "On Grid", "average_bill": 4000, "estimated_base_load": 400}))
    for frequency in ("Monthly", "BI-Monthly", "monthly", "", None, "Weekly"):
        add(f"frequency {frequency!r}", advanced_body({**existing, "average_bill": 2500, "bill_frequency": frequency}, devices=[{"device_type": "TV", "no_of_units": 2, "daily_usage": 5}]))
    # every device type, the built-in light, unknown names
    for name in DEVICE_NAMES:
        add(f"device {name}", advanced_body({**existing, "average_bill": 2000}, devices=[{"device_type": name, "no_of_units": 2, "daily_usage": 4}]))
    for name in ("Light", "light", "LIGHT", "ac 1 ton", "tv", "AC_1 ton", "AC%", "Unknown Gadget", "", None, " TV", "Tv\u0000"):
        add(f"device name {name!r}", advanced_body({**existing, "average_bill": 2000}, devices=[{"device_type": name, "no_of_units": 3, "daily_usage": 6}]))
    for label, device_text in (
        ("number name", '{"device_type": 5, "no_of_units": 1, "daily_usage": 2}'),
        ("list name", '{"device_type": ["TV"], "no_of_units": 1, "daily_usage": 2}'),
        ("units as text", '{"device_type": "TV", "no_of_units": "3", "daily_usage": "4.5"}'),
        ("units fractional", '{"device_type": "TV", "no_of_units": 2.9, "daily_usage": 3}'),
        ("units bad text", '{"device_type": "TV", "no_of_units": "two", "daily_usage": 3}'),
        ("usage bad text", '{"device_type": "TV", "no_of_units": 1, "daily_usage": "all day"}'),
        ("usage null", '{"device_type": "TV", "no_of_units": 1, "daily_usage": null}'),
        ("units null", '{"device_type": "TV", "no_of_units": null, "daily_usage": 1}'),
        ("defaults only", '{"device_type": "TV"}'),
        ("negative usage", '{"device_type": "Heater", "no_of_units": 1, "daily_usage": -10}'),
        ("huge usage", '{"device_type": "Heater", "no_of_units": 1, "daily_usage": 1e300}'),
        ("device not object", '"TV"'),
    ):
        add(f"device {label}", advanced_body(raw_specs=raw({**existing, "average_bill": 3000}), raw_usage='{"usage_electronic_devices": [' + device_text + "]}"))
    for label, usage_text in (
        ("devices null", '{"usage_electronic_devices": null}'),
        ("devices object", '{"usage_electronic_devices": {"device_type": "TV"}}'),
        ("usage list", "[]"),
        ("usage null", "null"),
        ("evs null", '{"electric_vehicles": null}'),
    ):
        add(f"usage {label}", advanced_body(raw_specs=raw({**existing, "average_bill": 3000}), raw_usage=usage_text))
    # device mixes that move the bill across the bill ranges
    mixes = [
        [{"device_type": "AC 1.5 ton", "no_of_units": 2, "daily_usage": 8}, {"device_type": "Refrigerator", "no_of_units": 1, "daily_usage": 24}],
        [{"device_type": "Heater", "no_of_units": 3, "daily_usage": 10}, {"device_type": "Electric Kettle", "no_of_units": 2, "daily_usage": 2}],
        [{"device_type": "AC 2 ton", "no_of_units": 4, "daily_usage": 12}, {"device_type": "Dishwasher", "no_of_units": 2, "daily_usage": 3}],
        [{"device_type": "Light", "no_of_units": 20, "daily_usage": 6}, {"device_type": "Fan", "no_of_units": 6, "daily_usage": 10}],
        [
            {"device_type": "Iron", "no_of_units": 1, "daily_usage": 1},
            {"device_type": "Toaster", "no_of_units": 1, "daily_usage": 0.5},
            {"device_type": "Microwave", "no_of_units": 1, "daily_usage": 0.75},
        ],
    ]
    for index, mix in enumerate(mixes):
        for bill in (1500, 6000):
            add(f"mix {index} bill {bill}", advanced_body({**existing, "average_bill": bill}, devices=mix))
            add(f"mix {index} bill {bill} monthly", advanced_body({**existing, "average_bill": bill, "bill_frequency": "Monthly"}, devices=mix))
    # EVs: every model, wrong case, unknown, value classes
    for model in EV_MODELS:
        add(f"ev {model}", advanced_body({**existing, "average_bill": 2500}, evs=[{"model": model, "daily_avg_km": 40, "no_of_vehicles": 1}]))
    for label, ev_text in (
        ("wrong case", '{"model": "tata nexon ev", "daily_avg_km": 40}'),
        ("unknown", '{"model": "Tesla Model 3", "daily_avg_km": 40}'),
        ("null model", '{"model": null, "daily_avg_km": 40}'),
        ("number model", '{"model": 7, "daily_avg_km": 40}'),
        ("two vehicles", '{"model": "Ather 450X", "daily_avg_km": 25.5, "no_of_vehicles": 2}'),
        ("km as text", '{"model": "Ather 450X", "daily_avg_km": "30"}'),
        ("km bad", '{"model": "Ather 450X", "daily_avg_km": "far"}'),
        ("vehicles bad", '{"model": "Ather 450X", "daily_avg_km": 30, "no_of_vehicles": "many"}'),
        ("km missing", '{"model": "MG ZS EV"}'),
        ("long trips", '{"model": "MG ZS EV", "daily_avg_km": 400, "no_of_vehicles": 3}'),
        ("nul model", '{"model": "MG ZS EV\\u0000", "daily_avg_km": 10}'),
    ):
        add(f"ev {label}", advanced_body(raw_specs=raw({**existing, "average_bill": 2500}), raw_usage='{"electric_vehicles": [' + ev_text + "]}"))
    two_evs = [{"model": "Tata Nexon EV", "daily_avg_km": 60, "no_of_vehicles": 2}, {"model": "Ola S1 Pro", "daily_avg_km": 30}]
    add("ev + devices mix", advanced_body({**existing, "average_bill": 5000}, devices=mixes[0], evs=two_evs))
    new_home_hybrid = {"home_type": "New Home", "grid_type": "Hybrid", "estimated_base_load": 250}
    add("ev + devices new home", advanced_body(new_home_hybrid, devices=mixes[1], evs=[{"model": "MG Comet EV", "daily_avg_km": 20}]))
    # hybrid: backup preferences and battery selection (batteries 4.61, 5.00, 5.12, 5.53 kWh)
    hybrid = {"home_type": "Existing Home", "grid_type": "Hybrid", "average_bill": 3500}
    for hours in (None, 0, "0", 1, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 24, 48, 100, "4", "abc", -2, "", [], True):
        add(f"hybrid backup {hours!r}", advanced_body(hybrid, preference={"backup_hours": hours}))
    backup_devices = [
        [],
        [{"device_type": "TV", "no_of_units": 1, "daily_usage": 3}],
        [{"device_type": "Refrigerator", "no_of_units": 1, "daily_usage": 5}],
        [{"device_type": "AC 1 ton", "no_of_units": 1, "daily_usage": 4}],
        [{"device_type": "AC 1.5 ton", "no_of_units": 2, "daily_usage": 6}],
        [{"device_type": "Heater", "no_of_units": 2, "daily_usage": 8}],
        [{"device_type": "Fan", "no_of_units": 4, "daily_usage": 6}, {"device_type": "Light", "no_of_units": 6, "daily_usage": 6}],
        [{"device_type": "Printer", "no_of_units": 1, "daily_usage": 1}, {"device_type": "Unknown", "no_of_units": 2, "daily_usage": 2}],
        [{"device_type": "Microwave", "no_of_units": 1, "daily_usage": 0.5}, {"device_type": "Washing Machine", "no_of_units": 1, "daily_usage": 1}],
    ]
    for index, devices in enumerate(backup_devices):
        for hours in (None, 2, 4, 6):
            add(f"hybrid devices {index} hours {hours}", advanced_body(hybrid, preference={"backup_hours": hours, "preference_electronic_devices": devices}))
    for label, preference_text in (
        ("device null name", '{"backup_hours": 3, "preference_electronic_devices": [{"device_type": null, "no_of_units": 1, "daily_usage": 1}]}'),
        ("device number name", '{"backup_hours": 3, "preference_electronic_devices": [{"device_type": 3}]}'),
        ("devices object", '{"backup_hours": 3, "preference_electronic_devices": {"device_type": "TV"}}'),
        ("devices text", '{"backup_hours": 3, "preference_electronic_devices": "TV"}'),
        ("devices null", '{"backup_hours": 3, "preference_electronic_devices": null}'),
        ("preference null", "null"),
        ("preference list", "[]"),
        ("units text", '{"backup_hours": 3, "preference_electronic_devices": [{"device_type": "TV", "no_of_units": "2", "daily_usage": "2"}]}'),
        ("usage bad", '{"backup_hours": 3, "preference_electronic_devices": [{"device_type": "TV", "daily_usage": "long"}]}'),
        ("hours only text", '{"backup_hours": "6", "preference_electronic_devices": [{"device_type": "TV", "no_of_units": 1, "daily_usage": 6}]}'),
        ("hours inf", '{"backup_hours": 1e400}'),
        ("huge load", '{"backup_hours": 5, "preference_electronic_devices": [{"device_type": "Heater", "no_of_units": 1000, "daily_usage": 5}]}'),
        (
            "fractional threshold",
            '{"backup_hours": 1, "preference_electronic_devices": [{"device_type": "Fan", "no_of_units": 1, "daily_usage": 0.1}, {"device_type": "TV", "no_of_units": 1, "daily_usage": 0.3}]}',
        ),
        ("enriched devices", '{"backup_hours": 4, "preference_electronic_devices": [{"device_type": "Water Pump", "no_of_units": 1, "daily_usage": 1}, {"device_type": "Router"}]}'),
    ):
        add(f"hybrid {label}", advanced_body(raw_specs=raw(hybrid), raw_preference=preference_text))
    for bill in (500, 3000, 9000, 16000, 30000, 90000):
        add(f"hybrid default backup bill {bill}", advanced_body({**hybrid, "average_bill": bill}))
    add(
        "hybrid new home",
        advanced_body({"home_type": "New Home", "grid_type": "Hybrid", "estimated_base_load": 600}, preference={"backup_hours": 5, "preference_electronic_devices": backup_devices[4]}),
    )
    # body shapes
    for label, text in (
        ("empty object", "{}"),
        ("list body", "[1]"),
        ("null body", "null"),
        ("string body", raw("Hybrid")),
        ("specifications null", '{"Specifications": null}'),
        ("specifications list", '{"Specifications": []}'),
        ("specifications text", '{"Specifications": "On Grid"}'),
        ("no specifications", '{"usageDetails": {}}'),
    ):
        add(label, text)
    return cases


# ── capture ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _json_default(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    raise TypeError(type(value))


def post(url: str, text: str) -> dict:
    request = urllib.request.Request(url, data=text.encode("utf-8"), method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - local legacy server
            status, content = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, content = error.code, error.read()
    text_content = content.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text_content)
    except ValueError:
        parsed = None
    error_class = None
    if status >= 500:
        match = re.search(r"<title>\s*([A-Za-z_.]+)", text_content) or re.match(r"\s*([A-Za-z_.]+) at /", text_content)  # HTML page, or the plain-text traceback
        error_class = match.group(1) if match else "unknown"
    return {"status": status, "response": parsed, "error_class": error_class}


def pincodes_used(cases_by_endpoint: dict[str, list[dict]]) -> list[str]:
    return sorted(set(KERALA_PINCODES) | set(UNKNOWN_PINCODES))


def export_rows(db: str) -> dict:
    import psycopg
    from psycopg.rows import dict_row

    rows = {}
    with psycopg.connect(host=os.environ.get("PGHOST", "localhost"), user=os.environ.get("PGUSER", "postgres"), dbname=db, row_factory=dict_row) as connection:
        connection.execute("SET default_transaction_read_only = on")
        for table in TABLES:
            rows[table] = connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()  # noqa: S608 - fixed table names
        rows["pincodes"] = connection.execute("SELECT * FROM pincodes WHERE pincode = ANY(%s) ORDER BY id", [pincodes_used({})]).fetchall()
    return rows


ENDPOINTS = {"basic": ("calculate-solar/", basic_cases), "basic_v2": ("calculate-solar-new/", basic_v2_cases), "advanced": ("calculate-solar-advanced/", advanced_cases)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--variant", required=True, choices=("uat", "enriched"))
    args = parser.parse_args()
    if args.variant == "enriched" and args.db == "legacy_goldenapp":
        raise SystemExit("the enriched corpus comes from a private restored copy, never the shared legacy_goldenapp")
    base = args.url.rstrip("/")
    target = HERE / args.variant
    target.mkdir(parents=True, exist_ok=True)
    rows = export_rows(args.db)
    (target / "legacy_rows.json").write_text(json.dumps({"source": {"db": args.db, "url": base}, "rows": rows}, indent=1, sort_keys=True, default=_json_default, ensure_ascii=False) + "\n")
    for key, (path, build) in ENDPOINTS.items():
        cases = build()
        names = [case["name"] for case in cases]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise SystemExit(f"duplicate case names in {key}: {sorted(duplicates)}")
        for case in cases:
            case.update(post(f"{base}/api/{path}", case["request"]))
        (target / f"corpus_{key}.json").write_text(json.dumps({"endpoint": f"POST /api/{path}", "source": base, "cases": cases}, indent=1, ensure_ascii=False) + "\n")
        statuses: dict[int, int] = {}
        for case in cases:
            statuses[case["status"]] = statuses.get(case["status"], 0) + 1
        print(f"{key}: {len(cases)} cases {statuses}")


if __name__ == "__main__":
    main()
