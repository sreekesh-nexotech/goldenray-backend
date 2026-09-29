#!/usr/bin/env python3
"""Write-path parity capture for ``POST /api/job-applications/`` (not run by pytest).

Runs every case in :data:`CASES` against a PRIVATE legacy main backend (a restored copy of ``legacy_goldenapp``,
never the shared UAT server), drives the Studio workflow endpoints (status, assign, notes, delete/archive, restore)
on two of the created applications with a Studio token signed by the UAT shared key, then exports:

* ``applications_cases.json`` — per case: the form, the legacy status, error dict or normalised stored values;
* ``backend_applications.json`` — ``job_application`` / ``_note`` / ``_event`` rows (``SELECT *``) with personal
  data masked, plus the resume/portfolio files under ``legacy/media/`` (synthetic bytes from ``sample_files``).

    python careers/tests/legacy/capture_applications.py --url http://127.0.0.1:18162 \\
        --db legacy_goldenapp_careers_reference --media /path/to/private/legacy/media
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import jwt
import psycopg
import requests
from psycopg.rows import dict_row

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sample_files  # noqa: E402

VALID = {
    "position": "General application",
    "full_name": "  Anu Thomas ",
    "email": " Anu.Thomas@Example.com ",
    "phone": "+91 98470-12345",
    "location": " Alappuzha ",
    "linkedin": "linkedin.com/in/anu-thomas",
    "portfolio_website": "anuthomas.dev",
    "current_company": "SunWorks",
    "current_role": "Technician",
    "total_experience": "1–3 years",
    "relevant_experience": "0–1 years",
    "current_salary": "Below ₹3 LPA",
    "expected_salary": "₹3–5 LPA",
    "notice_period": "1 month",
    "heard_about_us": "LinkedIn",
    "availability": "Immediately",
    "cover_note": "I like solar.",
    "declaration_accepted": "true",
    "website": "",
}
RESUME = {"resume": ("cv.pdf", "pdf")}


def case(name, *, drop=(), files=None, **fields):
    form = {key: value for key, value in {**VALID, **fields}.items() if key not in drop}
    return {"name": name, "form": form, "files": RESUME if files is None else files}


CASES = [
    case("valid_general"),
    case(
        "valid_minimal",
        drop=(
            "position",
            "portfolio_website",
            "current_company",
            "current_role",
            "total_experience",
            "relevant_experience",
            "current_salary",
            "expected_salary",
            "notice_period",
            "heard_about_us",
            "availability",
            "cover_note",
            "website",
        ),
    ),
    case("valid_posting", position="Solar Installation Engineer", position_id="1", position_title="Solar Installation Engineer", department_name="Engineering"),
    case("valid_docx_resume_and_doc_portfolio", files={"resume": ("cv.docx", "docx"), "portfolio_file": ("work.doc", "doc")}),
    case("valid_phone_with_91", phone="919847012345", linkedin="https://in.linkedin.com/in/anu", portfolio_website="https://anu.example"),
    case("valid_blank_optionals", position="", total_experience="", portfolio_website=""),
    case("valid_general_with_area_of_interest", department_name="Sales"),
    case("blank_full_name", full_name="   "),
    case("missing_full_name", drop=("full_name",)),
    case("bad_email", email="not-an-email"),
    case("short_phone", phone="12345"),
    case("phone_not_mobile", phone="5847012345"),
    case("blank_location", location=""),
    case("linkedin_other_host", linkedin="https://example.com/anu"),
    case("missing_linkedin", drop=("linkedin",)),
    case("bad_total_experience", total_experience="10 years"),
    case("bad_expected_salary", expected_salary="a lot"),
    case("bad_notice_period", notice_period="6 months"),
    case("bad_heard_about", heard_about_us="Newspaper"),
    case("cover_note_too_long", cover_note="x" * 3001),
    case("availability_too_long", availability="x" * 33),
    case("position_too_long", position="x" * 201),
    case("declaration_false", declaration_accepted="false"),
    case("declaration_missing", drop=("declaration_accepted",)),
    case("resume_missing", files={}),
    case("resume_txt", files={"resume": ("cv.txt", "txt")}),
    case("resume_too_big", files={"resume": ("cv.pdf", "big_pdf")}),
    case("portfolio_txt", files={"resume": ("cv.pdf", "pdf"), "portfolio_file": ("work.txt", "txt")}),
    case("honeypot", website="http://spam.example"),
    case("position_id_not_a_number", position_id="abc"),
    # Approved differences (docs/decisions/careers-reference.md): the platform sniffs content and only accepts
    # applications for PUBLISHED postings; the legacy form checked the file name and accepted any position id.
    case("fake_pdf_content", files={"resume": ("cv.pdf", "fake_pdf")}),
    case("closed_posting", position="Store Keeper", position_id="6", position_title="Store Keeper", department_name="Operations"),
]
STORED_FIELDS = [
    "position",
    "position_id",
    "position_title",
    "department_name",
    "status",
    "full_name",
    "email",
    "phone",
    "location",
    "linkedin",
    "portfolio_website",
    "current_company",
    "current_role",
    "total_experience",
    "relevant_experience",
    "current_salary",
    "expected_salary",
    "notice_period",
    "heard_about_us",
    "availability",
    "cover_note",
    "declaration_accepted",
]


def _json_default(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    raise TypeError(type(value))


def studio_token(key: str) -> str:
    payload = {"username": "hr.parity", "modules": ["applications"], "permissions": {"applications": ["view", "edit", "archive"]}, "exp": int(time.time()) + 3600, "jti": uuid.uuid4().hex}
    return jwt.encode(payload, key, algorithm="HS256")


def post_case(base: str, item: dict) -> dict:
    files = {field: (name, sample_files.build(kind)) for field, (name, kind) in item["files"].items()}
    response = requests.post(f"{base}/api/job-applications/", data=item["form"], files=files, timeout=60)
    body = response.json()
    result = {"name": item["name"], "form": item["form"], "files": item["files"], "status": response.status_code}
    if response.status_code == 201:
        data = body["data"]
        result["id"] = data["id"]
        result["stored"] = {field: data.get(field) for field in STORED_FIELDS}
        result["files_stored"] = {"resume": bool(data.get("resume")), "portfolio_file": bool(data.get("portfolio_file"))}
    else:
        result["errors"] = body.get("errors")
    return result


def workflow(base: str, token: str, first: int, second: int) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    steps = [
        (first, "status", {"status": "reviewing", "note": "Good CV"}),
        (first, "notes", {"body": "Call back on Monday."}),
        (first, "assign", {"position_id": 1, "position_title": "Solar Installation Engineer", "department_name": "Engineering"}),
        (first, "status", {"status": "interview"}),
        (first, "status", {"status": "selected", "note": "Offer sent"}),
        (second, "status", {"status": "rejected", "note": "Not a fit"}),
        (second, "notes", {"body": "Keep for the talent pool."}),
    ]
    log = []
    for pk, name, body in steps:
        response = requests.post(f"{base}/api/job-applications/{pk}/{name}/", json=body, headers=headers, timeout=30)
        log.append({"id": pk, "action": name, "body": body, "status": response.status_code})
    response = requests.delete(f"{base}/api/job-applications/{second}/", headers=headers, timeout=30)
    log.append({"id": second, "action": "delete", "status": response.status_code})
    response = requests.post(f"{base}/api/job-applications/{second}/restore/", headers=headers, timeout=30)
    log.append({"id": second, "action": "restore", "status": response.status_code})
    response = requests.delete(f"{base}/api/job-applications/{second}/", headers=headers, timeout=30)
    log.append({"id": second, "action": "delete", "status": response.status_code})
    return log


def mask(rows: list[dict]) -> list[dict]:
    """Synthetic data already, masked anyway: no real name, e-mail, phone or profile URL leaves the capture."""
    masked = []
    for index, row in enumerate(rows, start=1):
        row = dict(row)
        row["full_name"] = f"Candidate {index}"
        row["email"] = f"candidate{index}@example.invalid"
        row["phone"] = f"98{index:08d}"
        row["linkedin"] = f"https://linkedin.com/in/candidate-{index}"
        if row.get("portfolio_website"):
            row["portfolio_website"] = f"https://candidate-{index}.example.invalid"
        masked.append(row)
    return masked


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--studio-key", default=os.environ.get("STUDIO_JWT_SIGNING_KEY", ""), help="the UAT STUDIO_JWT_SIGNING_KEY (legacy-env.sh)")
    args = parser.parse_args()
    if not args.studio_key:
        raise SystemExit("Pass --studio-key or export STUDIO_JWT_SIGNING_KEY (the private legacy server's key).")
    if args.db in ("legacy_goldenapp", "GoldenApp"):
        raise SystemExit("Refusing to write to the shared legacy database; restore a private copy.")
    base = args.url.rstrip("/")

    results = [post_case(base, item) for item in CASES]
    created = [result["id"] for result in results if result["status"] == 201]
    log = workflow(base, studio_token(args.studio_key), created[0], created[1])

    with psycopg.connect(host=os.environ.get("PGHOST", "localhost"), user=os.environ.get("PGUSER", "postgres"), dbname=args.db, row_factory=dict_row) as connection:
        applications = connection.execute("SELECT * FROM job_application ORDER BY id").fetchall()
        notes = connection.execute("SELECT * FROM job_application_note ORDER BY id").fetchall()
        events = connection.execute("SELECT * FROM job_application_event ORDER BY id").fetchall()

    media_target = HERE / "media"
    shutil.rmtree(media_target, ignore_errors=True)
    for row in applications:
        for field in ("resume", "portfolio_file"):
            if row.get(field):
                source = args.media / row[field]
                destination = media_target / row[field]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)

    (HERE / "applications_cases.json").write_text(json.dumps({"source": {"url": base, "db": args.db}, "cases": results, "workflow": log}, indent=1, ensure_ascii=False, default=_json_default) + "\n")
    (HERE / "backend_applications.json").write_text(
        json.dumps({"job_application": mask(applications), "job_application_note": notes, "job_application_event": events}, indent=1, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n"
    )
    print(f"{len(results)} cases ({len(created)} created), {len(log)} workflow steps, {len(applications)} applications exported")


if __name__ == "__main__":
    main()
