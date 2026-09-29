"""Parity: the public job-positions payloads equal the legacy CMS delivery after importing the same data.

Goldens (``careers/tests/legacy/cms_<scenario>.json``) were recorded by ``capture_cms.py``:

* ``shared`` — the seeded UAT CMS (``legacy_blog_cms`` on :18009);
* ``enriched`` — a private copy with edge cases added (SEO fields and ``noindex``, ``schema_extra`` that tries to
  override record facts, blank lines and ``\\r\\n`` in list fields, an inactive department with a live posting, a
  department whose only posting is closed, sort-order ties, archived and closed postings, company name and careers
  intro set).

The CMS rows are loaded through ``careers.services.legacy_import`` and every recorded response is replayed against
``/api/public/v1/``. The only transformation is the documented one: the legacy integer ``id`` becomes the posting's
``uid`` (resolved through ``core_legacy_map``). Everything else must be identical.
"""

import json
from pathlib import Path

import pytest

from careers.models import JobPosition
from careers.services.legacy_import import CMS, import_departments, import_positions, mapped_id
from company.models import CompanyProfile

pytestmark = pytest.mark.django_db
LEGACY = Path(__file__).resolve().parent / "legacy"
SCENARIOS = ["shared", "enriched"]


def load(scenario: str) -> dict:
    return json.loads((LEGACY / f"cms_{scenario}.json").read_text())


def seed(recorded: dict) -> None:
    site = recorded["site"]
    CompanyProfile.objects.create(
        trade_name=site["company_name"],
        legal_name="",
        website=site["frontend_base_url"],
        careers_accepting_general_applications=site["careers_accepting_general_applications"],
        careers_intro=site["careers_intro"],
    )
    departments = import_departments(recorded["careers_department"])
    positions = import_positions(recorded["careers_job_position"])
    assert departments["violations"] == [] and positions["violations"] == []


def uid_for(legacy_id: int) -> str:
    return str(JobPosition.all_objects.get(pk=mapped_id(CMS, "careers_job_position", legacy_id)).uid)


def expected(body: dict) -> dict:
    """The legacy payload with the integer ``id`` replaced by the mapped ``uid`` (DV-47)."""

    def card(item: dict) -> dict:
        item = dict(item)
        item["uid"] = uid_for(item.pop("id"))
        return item

    data = body["data"]
    return {**body, "data": [card(item) for item in data] if isinstance(data, list) else card(data)}


def new_path(key: str) -> tuple[str, dict]:
    if key.startswith("list"):
        params = dict(part.split("=", 1) for part in key.split("?", 1)[1].split("&")) if "?" in key else {}
        return "/api/public/v1/job-positions/", params
    return f"/api/public/v1/job-positions/{key.split('/', 1)[1]}/", {}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_recorded_response_is_reproduced(api_client, scenario):
    recorded = load(scenario)
    seed(recorded)
    assert recorded["responses"], "no goldens recorded"
    for key, legacy in recorded["responses"].items():
        path, params = new_path(key)
        response = api_client.get(path, params)
        assert response.status_code == legacy["status"], key
        if legacy["status"] == 200:
            assert response.json() == expected(legacy["body"]), key
        else:
            assert response.json()["code"] == "not_found", key


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_reimport_is_idempotent(scenario):
    recorded = load(scenario)
    seed(recorded)
    again = import_positions(recorded["careers_job_position"])
    assert again["created"] == 0 and again["updated"] == 0 and again["skipped"] == len(recorded["careers_job_position"])
    assert JobPosition.all_objects.count() == len(recorded["careers_job_position"])
