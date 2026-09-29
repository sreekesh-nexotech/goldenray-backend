"""Capture the legacy main backend's behaviour for the leads/customers parity tests (run by hand; never in CI).

    python leads/tests/fixtures/capture_legacy.py \\
        --shared http://127.0.0.1:18012 --shared-db legacy_goldenapp \\
        --private http://127.0.0.1:18137 --private-db legacy_goldenapp_leads_customers

* ``GET installation-stats/`` variants are captured from the shared UAT server (read-only) and from a private copy
  enriched with extra installations (other years, statuses, multi-district and unknown pincodes).
* Every ``POST`` (validation errors, accepted submissions, the OTP flow with the UAT's fake Twilio credentials) goes
  to the **private** server only, so the shared ``legacy_goldenapp`` database is never written.
* Rows are exported read-only (``SELECT``) and personal data is masked before it is written to the fixtures.

The legacy throttle keys on the raw ``X-Forwarded-For`` header, so each request carries its own address except in the
throttle case, which records the 6th request inside a minute.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

import psycopg
import requests

HERE = Path(__file__).resolve().parent / "legacy_backend"
STATS_QUERIES = [
    None,
    "",
    "688008",
    "688012",
    "688001",
    "682016",
    "682001",
    "689122",
    "686102",
    "685533",
    "695001",
    "680001",
    "673001",
    "670511",
    "682999",
    "110001",
    "999999",
    "abc",
    "68",
    "6880081",
    " 688008",
    "688008 ",
]
_counter = {"n": 0}


def _xff() -> dict:
    _counter["n"] += 1
    return {"X-Forwarded-For": f"198.51.100.{_counter['n'] % 250 + 1}, 10.9.{_counter['n'] // 250}.1"}


def _body(response: requests.Response):
    try:
        return response.json()
    except ValueError:
        return {"_text": response.text[:500]}


def capture_stats(base: str) -> list[dict]:
    results = []
    for pincode in STATS_QUERIES:
        params = None if pincode is None else {"pincode": pincode}
        response = requests.get(f"{base}/api/installation-stats/", params=params, headers=_xff(), timeout=20)
        results.append({"pincode": pincode, "status": response.status_code, "body": _body(response)})
    repeated = requests.get(f"{base}/api/installation-stats/?pincode=688008&pincode=682016", headers=_xff(), timeout=20)
    results.append({"pincode": ["688008", "682016"], "status": repeated.status_code, "body": _body(repeated)})
    upper = requests.get(f"{base}/api/installation-stats/?PINCODE=688008", headers=_xff(), timeout=20)
    results.append({"pincode": None, "raw_query": "PINCODE=688008", "status": upper.status_code, "body": _body(upper)})
    return results


def _post(base: str, path: str, payload, *, headers: dict | None = None) -> dict:
    response = requests.post(f"{base}{path}", json=payload, headers={**_xff(), **(headers or {})}, timeout=30)
    return {"request": payload, "status": response.status_code, "body": _body(response)}


LEAD_CASES = {
    "empty": {},
    "blank_name": {"name": "   ", "phone_number": "9876500001", "source": "footer"},
    "short_phone": {"name": "Asha", "phone_number": "12345", "source": "footer"},
    "landline_like_phone": {"name": "Asha", "phone_number": "5876500001", "source": "footer"},
    "unknown_source": {"name": "Asha", "phone_number": "9876500001", "source": "bogus"},
    "details_not_object": {"name": "Asha", "phone_number": "9876500001", "details": "x"},
    "details_nested": {"name": "Asha", "phone_number": "9876500001", "details": {"a": {"b": 1}}},
    "details_too_many": {"name": "Asha", "phone_number": "9876500001", "details": {f"k{i}": i for i in range(31)}},
    "name_too_long": {"name": "A" * 256, "phone_number": "9876500001"},
    "page_too_long": {"name": "Asha", "phone_number": "9876500001", "page": "/" + "p" * 255},
    "footer": {"name": "Footer Lead", "phone_number": "9876500011", "source": "footer", "page": "/"},
    "home_booking": {"name": "Booking Lead", "phone_number": "+91 98765-00012", "source": "home_booking", "page": "/"},
    "contact_page": {"name": "Contact Lead", "phone_number": "919876500013", "source": "contact_page", "page": "/contact-us", "details": {"Pincode": "682016", "Property type": "Residential"}},
    "group_purchase": {
        "name": "Group Lead",
        "phone_number": "9876500014",
        "source": "group_purchase",
        "page": "/group-purchase",
        "details": {"Email": "group@example.com", "Role": "Organiser", "District": "Ernakulam", "Locality": "Kakkanad", "Estimated homes": "25", "Monthly bill": "3000", "Group name": "Green Flats"},
    },
    "quotation": {
        "name": "Quote Lead",
        "phone_number": "9876500015",
        "source": "quotation",
        "page": "/solar-calculator",
        "details": {"Address": "12 Beach Road", "Pincode": "695001", "Monthly bill": "₹4500", "System size": "5 kW", "Subsidy eligibility": "Yes", "Language": "ml", "Empty": "", "Nothing": None},
    },
    "leading_zero_phone": {"name": "Zero Lead", "phone_number": "09876500018", "source": "footer"},
    "default_source": {"name": "No Source", "phone_number": "9876500016"},
    "long_detail_value": {"name": "Long Detail", "phone_number": "9876500017", "source": "other", "details": {"Note": "x" * 2100, "Count": 3, "Flag": True, "Ratio": 1.5}},
    # Added by the review (captured from a fresh private copy): the legacy rule drops every "+".
    "plus_prefix_phone": {"name": "Plus", "phone_number": "+9876500051", "source": "footer"},
    "plus_inside_phone": {"name": "Plus2", "phone_number": "98765+00058", "source": "quotation"},
}
AFFILIATE_VALID = {"full_name": "Partner One", "phone": "9876500021", "email": "Partner.One@Example.com ", "profession": "Real Estate Agent", "district": "Ernakulam"}
AFFILIATE_CASES = {
    "empty": {},
    "bad_email": {**AFFILIATE_VALID, "email": "not-an-email"},
    "bad_profession": {**AFFILIATE_VALID, "profession": "Astronaut"},
    "bad_district": {**AFFILIATE_VALID, "district": "Chennai"},
    "bad_phone": {**AFFILIATE_VALID, "phone": "12345"},
    "blank_name": {**AFFILIATE_VALID, "full_name": "  "},
    "honeypot": {**AFFILIATE_VALID, "website": "http://spam.example"},
    "valid": AFFILIATE_VALID,
    "valid_other": {"full_name": "Partner Two", "phone": "+91 98765 00022", "email": "two@example.com", "profession": "Society / RWA Representative", "district": "Thiruvananthapuram", "website": ""},
}
WARRANTY_VALID = {"full_name": "Owner One", "phone": "9876500031", "issue_type": "Inverter Fault", "description": "  Inverter shows error E02  "}
WARRANTY_CASES = {
    "empty": {},
    "bad_issue": {**WARRANTY_VALID, "issue_type": "Roof Leak"},
    "long_description": {**WARRANTY_VALID, "description": "d" * 2001},
    "bad_phone": {**WARRANTY_VALID, "phone": "98765"},
    "honeypot": {**WARRANTY_VALID, "website": "x"},
    "valid": WARRANTY_VALID,
    "valid_no_description": {"full_name": "Owner Two", "phone": "9876500032", "issue_type": "KSEB / Net Metering"},
}
SEND_OTP_CASES = {
    "empty": {},
    "phone_too_long": {"phone_number": "9" * 21},
    "fake_twilio": {"phone_number": "9876500041", "name": "Otp Lead"},
    "repeat_within_30_days": {"phone_number": "9876500041", "name": "Otp Lead"},
    "foreign_number": {"phone_number": "+15005550006"},
}
VERIFY_OTP_CASES = {
    "empty": {},
    "code_too_long": {"phone_number": "9876500041", "code": "12345678901"},
    "fake_twilio": {"phone_number": "9876500041", "code": "123456", "name": "Otp Lead"},
}


def capture_forms(base: str) -> dict:
    forms = {
        "lead_collection_home": {name: _post(base, "/api/lead-collection-home/", payload) for name, payload in LEAD_CASES.items()},
        "affiliate_applications": {name: _post(base, "/api/affiliate-applications/", payload) for name, payload in AFFILIATE_CASES.items()},
        "warranty_service_requests": {name: _post(base, "/api/warranty-service-requests/", payload) for name, payload in WARRANTY_CASES.items()},
        "send_otp": {name: _post(base, "/api/send-otp/", payload) for name, payload in SEND_OTP_CASES.items()},
        "verify_otp": {name: _post(base, "/api/verify-otp/", payload) for name, payload in VERIFY_OTP_CASES.items()},
    }
    # Throttle: the legacy scope allows 5 affiliate submissions per minute per X-Forwarded-For value.
    fixed = {"X-Forwarded-For": f"203.0.113.{dt.datetime.now().second + 100}"}  # an address no earlier run used
    statuses = [requests.post(f"{base}/api/warranty-service-requests/", json={}, headers=fixed, timeout=20).status_code for _ in range(6)]
    forms["warranty_throttle_statuses_same_ip"] = statuses
    return forms


def _rows(dsn: str, sql: str) -> list[dict]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(sql)
        names = [column.name for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def _jsonable(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if hasattr(value, "as_integer_ratio") and not isinstance(value, (int, float)):
        return str(value)
    return value


def _dump(name: str, rows) -> None:
    (HERE / name).write_text(json.dumps(rows, indent=1, ensure_ascii=False, default=_jsonable) + "\n")


def _mask_installation(row: dict) -> dict:
    return {**row, "customer_name": f"Customer {row['id']}", "phone_number": f"+9190000{row['id']:05d}", "address": f"Address {row['id']}"}


def _digest(value: str) -> int:
    return int(hashlib.sha256(value.encode()).hexdigest(), 16)


def _mask_person(row: dict, name_key: str, phone_key: str) -> dict:
    """Same person → same mask in every table (a mirrored enquiry still matches its source row)."""
    masked = {**row, name_key: f"Person {_digest(row[name_key].strip()) % 10000:04d}"}
    if row.get(phone_key):
        masked[phone_key] = f"98{_digest(row[phone_key][-10:]) % 10**8:08d}"
    return masked


def export(shared_dsn: str, private_dsn: str) -> None:
    _dump("customer_installations_uat.json", [_mask_installation(row) for row in _rows(shared_dsn, "SELECT * FROM customer_installations ORDER BY id")])
    _dump("customer_installations_enriched.json", [_mask_installation(row) for row in _rows(private_dsn, "SELECT * FROM customer_installations ORDER BY id")])
    pincodes = _rows(shared_dsn, "SELECT pincode, district, min(id) AS first_id FROM pincodes GROUP BY pincode, district ORDER BY pincode, min(id)")
    _dump("pincodes.json", [[row["pincode"], row["district"], row["first_id"]] for row in pincodes])
    _dump("solar_installations.json", _rows(shared_dsn, "SELECT * FROM solar_installations ORDER BY id"))
    _dump("solar_installation_new.json", _rows(shared_dsn, "SELECT * FROM solar_installation_new ORDER BY id"))
    _dump("lead_collection_home.json", [_mask_person(row, "name", "phone_number") for row in _rows(private_dsn, "SELECT * FROM lead_collection_home ORDER BY id")])
    _dump("affiliate_application.json", [_mask_person(row, "full_name", "phone") for row in _rows(private_dsn, "SELECT * FROM affiliate_application ORDER BY id")])
    _dump("warranty_service_request.json", [_mask_person(row, "full_name", "phone") for row in _rows(private_dsn, "SELECT * FROM warranty_service_request ORDER BY id")])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shared", default="http://127.0.0.1:18012")
    parser.add_argument("--shared-db", default="legacy_goldenapp")
    parser.add_argument("--private", default="http://127.0.0.1:18137")
    parser.add_argument("--private-db", default="legacy_goldenapp_leads_customers")
    parser.add_argument("--export-only", action="store_true", help="re-export the rows without sending requests")
    args = parser.parse_args()
    dsn = "host=localhost user=postgres password=postgres dbname={}"
    HERE.mkdir(parents=True, exist_ok=True)
    if args.export_only:
        export(dsn.format(args.shared_db), dsn.format(args.private_db))
        return
    stats = {"captured_on": dt.date.today().isoformat(), "timezone": "Asia/Kolkata", "uat": capture_stats(args.shared), "enriched": capture_stats(args.private)}
    _dump("installation_stats.json", stats)
    _dump("forms.json", capture_forms(args.private))
    export(dsn.format(args.shared_db), dsn.format(args.private_db))


if __name__ == "__main__":
    main()
