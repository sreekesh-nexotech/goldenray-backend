"""``import_flarize`` (PLAN §7.4) end to end on the committed Flarize fixtures, then ``verify_migration`` #1, #2, #4, #8, #9
and #12 — and each new check failing on the defect it exists for."""

from __future__ import annotations

import json
import shutil
from decimal import Decimal

import pytest
from django.core.management.base import CommandError

from accounts.models import User
from accounts.services.seeds import seed_roles
from company.models import BankAccount
from company.services.profile import current_profile
from core.models import LegacyMap
from media.models import MediaAsset
from migrations_tools.services import flarize as flarize_plan
from migrations_tools.tests.conftest import run
from migrations_tools.tests.ops_fixtures import FLARIZE_MEDIA, OPS, flarize_tables, write_fixture
from packs.models import PackRelease, ReleasePack
from pricing.models import PriceRelease
from projects.models import Project
from quotations.models import Quotation, Version

pytestmark = pytest.mark.django_db


@pytest.fixture
def source(tmp_path):
    return write_fixture(tmp_path / "flarize.json", flarize_tables())


def _import(source, *args):
    return run("import_flarize", "--source-fixture", str(source), "--media-root", str(FLARIZE_MEDIA), *args)


def test_every_plan_row_imports_releases_publish_and_a_rerun_changes_nothing(source, tmp_path):
    """One full import (the importers are slow); every assertion below runs on its result."""
    seed_roles()
    report = tmp_path / "report.json"
    out = _import(source, "--json", str(report))
    data = json.loads(report.read_text())
    assert data["ok"] and [step["step"] for step in data["steps"]] == [step.name for step in flarize_plan.PLAN.steps]
    check_users_and_company()
    check_cms_assets(data)
    check_releases_and_business_defaults(data, out)
    check_transactional_rows()
    again = _import(source)
    assert "totals: created 0, updated 0" in again
    check_verification(source, tmp_path)


def check_users_and_company():
    sales = User.objects.get(pk=LegacyMap.objects.get(source_system="FLARIZE", source_table="users.json", source_id="sales-001").target_id)
    assert sales.role.slug == "sales-executive" and sales.title == "Sales" and sales.must_reset_password and not sales.has_usable_password()
    engineer = User.objects.get(email="flarize.user5@example.com")
    assert engineer.role.slug == "engineering"  # users.json spells ENGINEERING "ENGINEER"
    profile = current_profile()
    assert profile.legal_name == "FLARIZE TECHNOLOGIES PRIVATE LIMITED" and profile.gstin == "32AAGCF7283D1ZR" and profile.website == "https://www.flarize.com"
    assert profile.phone_e164 == "+919000000001"
    assert not BankAccount.objects.exists()  # D-9: bank details are entered by staff, never imported


def check_cms_assets(data):
    step = next(step for step in data["steps"] if step["step"] == "flarize.cms_assets")
    result = step["results"]["cms-state.json:assets"]
    assert result["created"] == 2 and {v["code"] for v in result["violations"]} == {"not_active", "page_designer_not_migrated"}
    assets = MediaAsset.objects.filter(folder="flarize-cms")
    assert assets.count() == 2 and all(asset.visibility == MediaAsset.Visibility.PRIVATE and len(asset.checksum_sha256) == 64 for asset in assets)


def check_releases_and_business_defaults(data, out):
    assert PriceRelease.objects.get().number == 1
    release = PackRelease.objects.get()
    assert release.number == 1 and ReleasePack.objects.filter(release=release).count() == 11
    releases = next(step for step in data["steps"] if step["step"] == "flarize.releases")["results"]["releases"]
    assert releases["created"] == 2 and sum(v["code"] == "pack_excluded" for v in releases["violations"]) == 79
    # B-3: the approved pack configuration's market rates win (228,000 → 229,000 for ongrid value 3 kW)
    b3 = data["business_defaults"]["B-3"]
    assert any("229000" in violation["message"] and "228000" in violation["message"] for violation in b3)
    assert "B-3 — The approved pack configuration's market rates win" in out
    assert "B-5 — " in out and data["business_defaults"]["B-5"]  # components a HYB slot never matches, listed


def check_transactional_rows():
    assert Quotation.objects.filter(legacy=True).count() == 7
    assert Version.objects.filter(legacy=True, status="ISSUED").exclude(document_payload_sha256="").count() == 6
    assert Project.objects.exists()


def check_verification(source, tmp_path):
    out = run("verify_migration", "--source", "flarize", "--flarize-fixture", str(source), "--offline", *[arg for n in (1, 2, 4, 8, 9, 12) for arg in ("--check", str(n))])
    assert "#8  PASS" in out and "11 packs equal to the Flarize reference" in out and "1 blocked by the checker" in out
    assert "#9  PASS" in out and "6 frozen documents" in out and "12 renders (en + ml) deterministic" in out
    assert "verification passed" in out
    # #8 fails on a released price that differs from the Flarize reference
    row = ReleasePack.objects.filter(release__number=1).first()
    ReleasePack.objects.filter(release=row.release, key=row.key).update(customer_price_incl_gst=row.customer_price_incl_gst + Decimal("1"))
    with pytest.raises(CommandError, match="#8"):
        run("verify_migration", "--source", "flarize", "--flarize-fixture", str(source), "--check", "8")
    ReleasePack.objects.filter(release=row.release, key=row.key).update(customer_price_incl_gst=row.customer_price_incl_gst)
    # #8 fails when the reference was generated from other Flarize files (a folder source is hashed)
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "pack-config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CommandError, match="#8"):
        run("verify_migration", "--source", "flarize", "--flarize-dir", str(folder), "--check", "8")
    # #9 fails when a frozen document no longer matches its source / its stored hash
    version = Version.objects.filter(legacy=True, status="ISSUED").first()
    payload = json.loads(json.dumps(version.document_payload))
    payload["tampered"] = True
    Version.objects.filter(pk=version.pk).update(document_payload=payload)
    with pytest.raises(CommandError, match="#9"):
        run("verify_migration", "--source", "flarize", "--flarize-fixture", str(source), "--check", "9")


def test_dry_run_and_folder_source(tmp_path):
    """A dry run writes nothing; the folder source reads each file and the cms-assets beside them."""
    seed_roles()
    folder = tmp_path / "data"
    folder.mkdir()
    tables = flarize_tables()
    for name in ("users.json", "cms-state.json", "company-profile.json"):
        (folder / name).write_text(json.dumps(tables[name][0]), encoding="utf-8")
    shutil.copytree(OPS / "flarize" / "cms-assets", folder / "cms-assets")
    steps = ("--only", "flarize.users", "--only", "flarize.cms_assets", "--only", "flarize.company")
    out = run("import_flarize", "--source-dir", str(folder), "--dry-run", *steps)
    assert "DRY RUN (rolled back)" in out and "users.json                         created     5" in out
    assert not LegacyMap.objects.filter(source_system="FLARIZE").exists() and not MediaAsset.objects.exists()
    out = run("import_flarize", "--source-dir", str(folder), *steps)
    assert "cms-state.json:assets              created     2" in out and MediaAsset.objects.filter(folder="flarize-cms").count() == 2
    assert LegacyMap.objects.filter(source_system="FLARIZE", source_table="users.json").count() == 5


def test_command_errors(tmp_path):
    with pytest.raises(CommandError, match="--source-dir"):
        run("import_flarize")
    with pytest.raises(CommandError, match="not a directory"):
        run("import_flarize", "--source-dir", str(tmp_path / "missing"))
    with pytest.raises(CommandError, match="not a readable JSON fixture"):
        run("import_flarize", "--source-fixture", str(tmp_path / "missing.json"))
    out = run("import_flarize", "--list-steps")
    assert "flarize.releases" in out and "catalog.json.bak" in out
