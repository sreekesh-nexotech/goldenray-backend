"""Write-path parity: POST /api/public/v1/job-applications/ accepts, refuses and normalises like the legacy form.

``legacy/applications_cases.json`` was recorded by ``capture_applications.py`` against a PRIVATE restored copy of the
legacy main backend (32 cases: every field rule, file rules, honeypot, success variants). Each case is replayed here
with the same form fields and the same file bytes (``legacy/sample_files.py``); the posting id is translated to the
posting's ``uid`` through the CMS import map (DV-47). The platform must:

* accept exactly the submissions the legacy form accepted and store the same normalised values;
* refuse exactly the ones it refused, with errors on the same fields and — where the rule is the legacy form's own
  text — the same messages;

except for the approved differences listed in :data:`APPROVED_DIFFERENCES` (docs/decisions/careers-reference.md).
"""

import json
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from careers.models import JobApplication, JobPosition
from careers.services.legacy_import import CMS, import_departments, import_positions, mapped_id
from careers.tests.legacy import sample_files

pytestmark = pytest.mark.django_db
LEGACY = Path(__file__).resolve().parent / "legacy"
URL = "/api/public/v1/job-applications/"
RECORDED = json.loads((LEGACY / "applications_cases.json").read_text())
CMS_SHARED = json.loads((LEGACY / "cms_shared.json").read_text())

#: case → (platform status, error code, reason)
APPROVED_DIFFERENCES = {
    "fake_pdf_content": (400, "unsupported_file_type", "the platform checks the file's content, the legacy form only its name"),
    "closed_posting": (400, "position_not_open", "only PUBLISHED postings accept applications (the legacy form accepted any position id)"),
}
#: error messages that differ only because the field changed type (integer CMS id → posting uid)
MESSAGE_DIFFERENCES = {("position_id_not_a_number", "position_id")}
STORED = {
    "full_name": "name",
    "email": "email",
    "location": "location",
    "linkedin": "linkedin",
    "portfolio_website": "portfolio_website",
    "current_company": "current_company",
    "current_role": "current_role",
    "total_experience": "total_experience",
    "relevant_experience": "relevant_experience",
    "current_salary": "current_salary",
    "expected_salary": "expected_salary",
    "notice_period": "notice_period",
    "heard_about_us": "heard_about_us",
    "availability": "availability",
    "cover_note": "cover_letter",
    "position": "position_label",
    "position_title": "position_title",
    "department_name": "department_name",
    "declaration_accepted": "declaration_accepted",
}


@pytest.fixture
def postings():
    import_departments(CMS_SHARED["careers_department"])
    import_positions(CMS_SHARED["careers_job_position"])


def translated_form(case: dict) -> dict:
    form = dict(case["form"])
    legacy_position = form.get("position_id")
    if legacy_position and legacy_position.isdigit():
        form["position_id"] = str(JobPosition.all_objects.get(pk=mapped_id(CMS, "careers_job_position", legacy_position)).uid)
    for field, (name, kind) in case["files"].items():
        form[field] = SimpleUploadedFile(name, sample_files.build(kind))
    return form


def test_all_cases_were_recorded():
    names = [case["name"] for case in RECORDED["cases"]]
    assert len(names) == len(set(names)) == 32
    assert set(APPROVED_DIFFERENCES) <= set(names)


@pytest.mark.parametrize("case", RECORDED["cases"], ids=[case["name"] for case in RECORDED["cases"]])
def test_case(api_client, postings, case):
    response = api_client.post(URL, translated_form(case), format="multipart")
    body = response.json()
    if case["name"] in APPROVED_DIFFERENCES:
        status, code, _reason = APPROVED_DIFFERENCES[case["name"]]
        assert case["status"] == 201 and response.status_code == status and body["code"] == code
        return
    assert response.status_code == case["status"], body
    if case["status"] != 201:
        assert set(body["errors"]) == set(case["errors"]), body
        for field, messages in case["errors"].items():
            if (case["name"], field) not in MESSAGE_DIFFERENCES:
                assert body["errors"][field] == messages, (field, body["errors"][field], messages)
        assert not JobApplication.all_objects.exists()
        return
    application = JobApplication.objects.get(uid=body["uid"])
    stored = case["stored"]
    for legacy_field, field in STORED.items():
        assert getattr(application, field) == stored[legacy_field], legacy_field
    assert application.phone_e164 == f"+91{stored['phone']}"
    assert application.status == {"new": "NEW"}[stored["status"]]
    if stored["position_id"]:
        assert application.position_id == mapped_id(CMS, "careers_job_position", stored["position_id"])
    else:
        assert application.position is None
    assert (application.resume is not None) == case["files_stored"]["resume"]
    assert (application.portfolio is not None) == case["files_stored"]["portfolio_file"]
