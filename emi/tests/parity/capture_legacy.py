#!/usr/bin/env python3
"""Record the legacy EMI calculator for the parity tests (run by hand; never in CI).

    PGPASSWORD=postgres python emi/tests/parity/capture_legacy.py --db legacy_goldenapp --url http://127.0.0.1:18012 --variant uat

    # the enriched private copy (never the shared database): see enrich_private.sql
    PGPASSWORD=postgres python emi/tests/parity/capture_legacy.py \\
        --db legacy_goldenapp_calculators_emi --url http://127.0.0.1:18161 --variant enriched

Writes ``emi/tests/parity/<variant>/``:

* ``legacy_rows.json`` — ``emi_system_size``, ``emi_subsidy_rule``, ``emi_interest_rate_rule``,
  ``emi_calculator_settings`` and ``emi_bank``, exported with ``SELECT *`` in a read-only transaction;
* ``config.json`` — ``GET /api/emi-calculator/config/``;
* ``corpus_calculate.json`` (``POST /api/emi-calculator/``) and ``corpus_quotation.json``
  (``POST /api/emi-calculator/quotation/``) — one case per input of a deterministic grid: the raw JSON request body
  (``request``), the legacy status, the parsed response (``response``) and, for a 500, the exception class.

Every request only reads (the settings row already exists, so ``EmiCalculatorSettings.load()`` never writes).
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
TABLES = ("emi_system_size", "emi_subsidy_rule", "emi_interest_rate_rule", "emi_calculator_settings", "emi_bank")


def raw(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def obj(**fields) -> str:
    return raw(fields)


def calculate_cases() -> list[dict]:  # noqa: C901 - a grid, not logic
    cases: list[dict] = []

    def add(name: str, text: str) -> None:
        cases.append({"name": name, "request": text})

    # sizes by legacy id and by capacity, every tenure
    for size_id in (1, 2, 3, 4, 5, 6, 7):  # 5-7 exist only in the enriched copy
        for tenure in (None, 1, 3, 5, 7, 10):
            fields = {"size_id": size_id}
            if tenure is not None:
                fields["tenure_years"] = tenure
            add(f"size {size_id} tenure {tenure}", raw(fields))
    for capacity in (3, 5, 8, 10, 3.0, 5.00001, 3.0000001, 2.9999999, 7, 1, 12, 0.5, 4, 6.5, 6.500001):
        add(f"capacity {capacity!r}", obj(capacity_kw=capacity))
    for label, text in (
        ("capacity text", '{"capacity_kw": "5"}'),
        ("capacity text decimal", '{"capacity_kw": "8.00"}'),
        ("capacity bad", '{"capacity_kw": "five"}'),
        ("capacity zero", '{"capacity_kw": 0}'),
        ("capacity zero + power", '{"capacity_kw": 0, "power_capacity": 10}'),
        ("power alias", '{"power_capacity": 8}'),
        ("capacity negative", '{"capacity_kw": -3}'),
        ("capacity inf", '{"capacity_kw": "inf"}'),
        ("capacity nan", '{"capacity_kw": "nan"}'),
        ("capacity 1e400", '{"capacity_kw": 1e400}'),
        ("capacity true", '{"capacity_kw": true}'),
        ("capacity list", '{"capacity_kw": [3]}'),
        ("capacity object", '{"capacity_kw": {"kw": 3}}'),
        ("capacity empty", '{"capacity_kw": ""}'),
        ("size and capacity", '{"size_id": 2, "capacity_kw": 10}'),
        ("installation alias", '{"installation_id": 3}'),
        ("id alias", '{"id": 4}'),
        ("size unknown", '{"size_id": 99}'),
        ("size unknown + capacity", '{"size_id": 99, "capacity_kw": 5}'),
        ("size zero", '{"size_id": 0}'),
        ("size text", '{"size_id": "2"}'),
        ("size fraction", '{"size_id": 3.7}'),
        ("size bad", '{"size_id": "two"}'),
        ("size true", '{"size_id": true}'),
        ("size huge", '{"size_id": 99999999999999999999999}'),
        ("empty object", "{}"),
        ("empty list", "[]"),
        ("list body", "[1]"),
        ("null body", "null"),
        ("string body", raw("size_id=1")),
        ("number body", "3"),
        ("bank ignored", '{"size_id": 1, "bank": "sbi", "bank_id": 2}'),
    ):
        add(label, text)
    # tenures
    for tenure_text in ("0", "11", "-1", "2.5", '"4"', '"4.5"', '"abc"', "true", "null", '""', "[5]", "1e400"):
        add(f"tenure {tenure_text}", '{"size_id": 2, "tenure_years": ' + tenure_text + "}")
    # interest rate adjustments (loan bands: ≤ ₹2L locked 5.75 %, above locked 8 %; default 9.5 % when nothing matches)
    for rate_text in ("1", "5", "5.75", "6", "7.9", "8", "9.5", "12", "18", "25", "-2", "0", '"8.5"', '"abc"', '"nan"', "true", "[8]"):
        for size_id in (1, 3):
            add(f"rate {rate_text} size {size_id}", '{"size_id": ' + str(size_id) + ', "interest_rate": ' + rate_text + "}")
    # price slider (bands: 3 kW 1.8L-5L, 5 kW 2.6L-6.5L, 8 kW 4.2L-7.5L, 10 kW 4.8L-9L)
    for size_id, prices in ((1, (100000, 180000, 200000, 222222.22, 230001, 350000, 500000, 600000)), (2, (250000, 260000, 300000.5, 650000, 700000)), (4, (480000, 555555.555, 900000, 1000000))):
        for price in prices:
            add(f"price {price} size {size_id}", obj(size_id=size_id, system_cost=price))
    for label, text in (
        ("price alias", '{"size_id": 3, "price": 500000}'),
        ("price zero", '{"size_id": 3, "system_cost": 0}'),
        ("price zero + alias", '{"size_id": 3, "system_cost": 0, "price": 450000}'),
        ("price negative", '{"size_id": 3, "system_cost": -5}'),
        ("price text", '{"size_id": 3, "system_cost": "600000"}'),
        ("price bad", '{"size_id": 3, "system_cost": "lots"}'),
        ("price huge", '{"size_id": 3, "system_cost": 1e30}'),
        ("price inf", '{"size_id": 3, "system_cost": "inf"}'),
        ("price tiny", '{"size_id": 3, "system_cost": 0.001}'),
    ):
        add(label, text)
    # down payment slider (10 %-90 %)
    for percent_text in ("0", "5", "10", "12.5", "33.333333", "50", "75", "90", "95", "-10", '"20"', '"abc"', "null", '"nan"'):
        for size_id in (1, 4):
            add(f"down payment {percent_text} size {size_id}", '{"size_id": ' + str(size_id) + ', "down_payment_percent": ' + percent_text + "}")
    # toggles
    toggles = ("true", "false", '"no"', '"0"', '"1"', '"yes"', '"Y"', "0", "1", "null", '" TRUE "', '"off"', "[]")
    for toggle in toggles:
        add(f"apply_down_payment {toggle}", '{"size_id": 2, "apply_down_payment": ' + toggle + ', "down_payment_percent": 40}')
        add(f"apply_subsidy {toggle}", '{"size_id": 1, "apply_subsidy": ' + toggle + "}")
    # combinations that move across the loan bands (and the rate-unlock suggestion)
    for size_id in (1, 2, 3, 4):
        for percent in (10, 20, 40, 60, 90):
            for subsidy in (True, False):
                add(f"combo size {size_id} dp {percent} subsidy {subsidy}", obj(size_id=size_id, down_payment_percent=percent, apply_subsidy=subsidy, tenure_years=7))
    for size_id, price, percent, rate in ((1, 210000, 10, 6), (2, 400000, 45, 10), (3, 700000, 70, 9), (4, 850000, 15, 16), (4, 480000, 90, 20)):
        add(
            f"full custom size {size_id} price {price}",
            obj(size_id=size_id, system_cost=price, down_payment_percent=percent, interest_rate=rate, tenure_years=10, apply_down_payment=True, apply_subsidy=True),
        )
    return cases


def quotation_cases() -> list[dict]:
    cases: list[dict] = []

    def add(name: str, text: str) -> None:
        cases.append({"name": name, "request": text})

    tiers = {"economy": {"system_cost": 210000, "subsidy": 78000}, "standard": {"system_cost": 265000, "subsidy": 78000}, "premium": {"system_cost": 342000.5, "subsidy": 78000}}
    for capacity in (1, 2, 2.5, 3, 3.3, 4, 5, 6, 8, 10, 12, 15):
        for tenure in (None, 1, 2, 5, 7, 10):
            fields = {"capacity_kw": capacity, "packages": tiers}
            if tenure is not None:
                fields["tenure_years"] = tenure
            add(f"capacity {capacity} tenure {tenure}", raw(fields))
    for cost in (1, 1000, 99999.99, 150000, 200000, 222222, 222222.22, 222222.23, 222222.225, 250000, 300000, 555555.55, 1000000, 5000000):
        for subsidy in (0, 30000, 78000, 78000.005, 300000):
            add(f"cost {cost} subsidy {subsidy}", raw({"capacity_kw": 3, "tenure_years": 10, "packages": {"only": {"system_cost": cost, "subsidy": subsidy}}}))
    for cost in range(100000, 1000001, 45000):  # across the ₹2L loan band for three sizes
        for capacity in (3, 5, 10):
            add(f"sweep {cost} capacity {capacity}", raw({"capacity_kw": capacity, "packages": {"economy": {"system_cost": cost, "subsidy": 78000}, "premium": {"system_cost": cost + 61000}}}))
    for label, package in (
        ("cost zero", {"system_cost": 0, "subsidy": 0}),
        ("cost negative", {"system_cost": -100, "subsidy": 0}),
        ("cost missing", {"subsidy": 78000}),
        ("cost text", {"system_cost": "250000", "subsidy": "78000"}),
        ("cost bad text", {"system_cost": "a lot"}),
        ("cost null", {"system_cost": None}),
        ("cost list", {"system_cost": [1]}),
        ("subsidy negative", {"system_cost": 250000, "subsidy": -5000}),
        ("subsidy bad", {"system_cost": 250000, "subsidy": "some"}),
        ("subsidy above cost", {"system_cost": 50000, "subsidy": 78000}),
        ("subsidy missing", {"system_cost": 250000}),
        ("empty package", {}),
    ):
        add(f"package {label}", raw({"capacity_kw": 5, "packages": {"p": package}}))
    for label, text in (
        ("cost huge", '{"capacity_kw": 5, "packages": {"p": {"system_cost": 1e30}}}'),
        ("cost inf", '{"capacity_kw": 5, "packages": {"p": {"system_cost": "inf"}}}'),
        ("cost nan", '{"capacity_kw": 5, "packages": {"p": {"system_cost": "nan"}}}'),
        ("subsidy inf", '{"capacity_kw": 5, "packages": {"p": {"system_cost": 250000, "subsidy": "inf"}}}'),
        ("package not object", '{"capacity_kw": 5, "packages": {"p": 250000}}'),
        ("second package bad", '{"capacity_kw": 5, "packages": {"a": {"system_cost": 250000}, "b": "x"}}'),
        ("packages list", '{"capacity_kw": 5, "packages": [{"system_cost": 250000}]}'),
        ("packages empty", '{"capacity_kw": 5, "packages": {}}'),
        ("packages missing", '{"capacity_kw": 5}'),
        ("packages null", '{"capacity_kw": 5, "packages": null}'),
        ("seven packages", raw({"capacity_kw": 5, "packages": {f"p{n}": {"system_cost": 200000 + n} for n in range(7)}})),
        ("six packages", raw({"capacity_kw": 5, "packages": {f"p{n}": {"system_cost": 200000 + n * 50000, "subsidy": 78000} for n in range(6)}})),
        ("long key", raw({"capacity_kw": 5, "packages": {"k" * 40: {"system_cost": 250000}}})),
        ("capacity missing", raw({"packages": tiers})),
        ("capacity zero", raw({"capacity_kw": 0, "packages": tiers})),
        ("capacity negative", raw({"capacity_kw": -1, "packages": tiers})),
        ("capacity text", raw({"capacity_kw": "5", "packages": tiers})),
        ("capacity bad", raw({"capacity_kw": "five", "packages": tiers})),
        ("capacity nan", raw({"capacity_kw": "nan", "packages": tiers})),
        ("capacity inf", raw({"capacity_kw": "inf", "packages": tiers})),
        ("capacity true", raw({"capacity_kw": True, "packages": tiers})),
        ("tenure zero", raw({"capacity_kw": 5, "tenure_years": 0, "packages": tiers})),
        ("tenure eleven", raw({"capacity_kw": 5, "tenure_years": 11, "packages": tiers})),
        ("tenure text", raw({"capacity_kw": 5, "tenure_years": "7", "packages": tiers})),
        ("tenure fraction", raw({"capacity_kw": 5, "tenure_years": 6.9, "packages": tiers})),
        ("tenure bad", raw({"capacity_kw": 5, "tenure_years": "long", "packages": tiers})),
        ("tenure empty", raw({"capacity_kw": 5, "tenure_years": "", "packages": tiers})),
        ("empty object", "{}"),
        ("empty list", "[]"),
        ("list body", "[1]"),
        ("null body", "null"),
        ("string body", raw("packages")),
    ):
        add(label, text)
    return cases


def _json_default(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    raise TypeError(type(value))


def call(url: str, text: str | None = None) -> dict:
    data = None if text is None else text.encode("utf-8")
    request = urllib.request.Request(url, data=data, method="GET" if text is None else "POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - local legacy server
            status, content = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, content = error.code, error.read()
    content_text = content.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(content_text)
    except ValueError:
        parsed = None
    error_class = None
    if status >= 500:
        match = re.search(r"<title>\s*([A-Za-z_.]+)", content_text) or re.match(r"\s*([A-Za-z_.]+) at /", content_text)  # HTML page, or the plain-text traceback
        error_class = match.group(1) if match else "unknown"
    return {"status": status, "response": parsed, "error_class": error_class}


def export_rows(db: str) -> dict:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(host=os.environ.get("PGHOST", "localhost"), user=os.environ.get("PGUSER", "postgres"), dbname=db, row_factory=dict_row) as connection:
        connection.execute("SET default_transaction_read_only = on")
        return {table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in TABLES}  # noqa: S608 - fixed table names


ENDPOINTS = {"calculate": ("emi-calculator/", calculate_cases), "quotation": ("emi-calculator/quotation/", quotation_cases)}


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
    config = call(f"{base}/api/emi-calculator/config/")
    (target / "config.json").write_text(json.dumps({"endpoint": "GET /api/emi-calculator/config/", "source": base, **config}, indent=1, ensure_ascii=False) + "\n")
    for key, (path, build) in ENDPOINTS.items():
        cases = build()
        names = [case["name"] for case in cases]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise SystemExit(f"duplicate case names in {key}: {sorted(duplicates)}")
        for case in cases:
            case.update(call(f"{base}/api/{path}", case["request"]))
        (target / f"corpus_{key}.json").write_text(json.dumps({"endpoint": f"POST /api/{path}", "source": base, "cases": cases}, indent=1, ensure_ascii=False) + "\n")
        statuses: dict[int, int] = {}
        for case in cases:
            statuses[case["status"]] = statuses.get(case["status"], 0) + 1
        print(f"{key}: {len(cases)} cases {statuses}")


if __name__ == "__main__":
    main()
