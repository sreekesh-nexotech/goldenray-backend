"""Fixtures of the migration tools: the committed legacy exports and a legacy CMS stand-in built from recorded goldens."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from django.core.management import call_command

from accounts.services.seeds import seed_roles
from migrations_tools.services.http import query_string

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CMS_FIXTURE = FIXTURES / "cms.json"
BACKEND_FIXTURE = FIXTURES / "backend.json"


@pytest.fixture(autouse=True)
def media_roots(settings, tmp_path):
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.PUBLIC_MEDIA_URL = "/media/public/"
    settings.FRONTEND_BASE_URL = "http://localhost:3000"  # the legacy CMS default the goldens were recorded with
    return tmp_path


def load_tables(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["tables"]


def run(command: str, *args, **options) -> str:
    out = io.StringIO()
    call_command(command, *args, stdout=out, **options)
    return out.getvalue()


@pytest.fixture
def imported(db):
    """Both sources imported from the committed fixtures into a freshly seeded platform."""
    seed_roles()
    cms = run("import_cms", "--source-fixture", str(CMS_FIXTURE))
    backend = run("import_backend", "--source-fixture", str(BACKEND_FIXTURE))
    return cms, backend


class GoldenLegacyCms:
    """The legacy CMS HTTP API as recorded from the shared UAT server (the blog, sitepages, faqs and careers goldens)."""

    def __init__(self):
        blog = ROOT / "blog/tests/fixtures/legacy_cms/golden"
        self.collections = {case["query"]: (case["status"], json.loads((blog / f"{case['name']}.json").read_bytes())) for case in json.loads((blog / "manifest.json").read_text())}
        self.pages = json.loads((ROOT / "sitepages/tests/fixtures/legacy_cms/uat/golden_page_content.json").read_text())
        self.faqs = json.loads((ROOT / "faqs/tests/fixtures/legacy_cms/uat/golden_faqs.json").read_text())
        self.positions = json.loads((ROOT / "careers/tests/legacy/cms_shared.json").read_text())["responses"]
        self.requests: list[tuple[str, dict]] = []
        self.overrides: dict = {}

    def get(self, path: str, params=None):
        params = dict(params or {})
        self.requests.append((path, params))
        if (path, json.dumps(params, sort_keys=True)) in self.overrides:
            return self.overrides[(path, json.dumps(params, sort_keys=True))]
        if path == "/api/articles":
            from urllib.parse import unquote

            return self.collections.get(unquote(query_string(params)), (599, None))
        if path in ("/api/page-content", "/api/faqs"):
            for case in self.pages if path == "/api/page-content" else self.faqs:
                if case["query"] == params:
                    return case["status"], case["body"]
            return 599, None
        if path == "/api/job-positions":
            key = "list" + (f"?department={params['department']}" if "department" in params else "")
        else:
            key = "detail/" + path.rsplit("/", 1)[1]
        recorded = self.positions.get(key)
        return (recorded["status"], recorded["body"]) if recorded else (599, None)

    def override(self, path: str, params: dict, response) -> None:
        self.overrides[(path, json.dumps(params, sort_keys=True))] = response


@pytest.fixture
def legacy_cms():
    return GoldenLegacyCms()
