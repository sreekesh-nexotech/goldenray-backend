"""``verify_migration`` (PLAN §7.6) on the committed exports: every check passes on a clean import and fails on the
defect it exists for. Delivery parity is compared with responses recorded from the legacy CMS (the goldens of the
blog, sitepages, faqs and careers packages), so check #3 here is real legacy-vs-new parity."""

import hashlib
import json

import pytest
from django.core.management.base import CommandError

from accounts.models import User
from accounts.services import legacy_import as accounts_import
from catalog.models import Component
from core.models import LegacyMap
from media.models import MediaAsset
from migrations_tools.services import parity
from migrations_tools.services.http import InProcessClient, unthrottled
from migrations_tools.services.source import TablesSource
from migrations_tools.services.verify import Verifier
from migrations_tools.tests.conftest import BACKEND_FIXTURE, CMS_FIXTURE, ROOT, load_tables, run
from pricing.models import Price

pytestmark = pytest.mark.django_db


@pytest.fixture
def corpus(tmp_path):
    """A small slice of the committed UAT calculator corpora (the full replay runs in the rehearsal)."""
    dirs = {"calculators": tmp_path / "calculators", "emi": tmp_path / "emi"}
    for kind, source in (("calculators", ROOT / "calculators/tests/parity/uat"), ("emi", ROOT / "emi/tests/parity/uat")):
        dirs[kind].mkdir()
        for path in source.glob("corpus_*.json"):
            data = json.loads(path.read_text())
            data["cases"] = [case for case in data["cases"] if case["status"] != 200][:3] + [case for case in data["cases"] if case["status"] == 200][:2]
            (dirs[kind] / path.name).write_text(json.dumps(data))
    (dirs["emi"] / "config.json").write_text((ROOT / "emi/tests/parity/uat/config.json").read_text())
    return dirs


def verifier(legacy_cms, corpus, **kwargs):
    sources = {"CMS": TablesSource(load_tables(CMS_FIXTURE)), "BACKEND": TablesSource(load_tables(BACKEND_FIXTURE))}
    return Verifier(sources=kwargs.pop("sources", sources), new=InProcessClient(), legacy_cms=legacy_cms, offline=True, corpus_dirs=corpus, **kwargs)


def results(check_verifier, *numbers):
    with unthrottled():
        return {result.number: result for result in check_verifier.run(set(numbers) or None)}


def test_a_clean_import_passes_every_check(imported, legacy_cms, corpus):
    outcome = results(verifier(legacy_cms, corpus, list_prices_as_release=True))
    assert {number: result.status for number, result in outcome.items()} == {number: "pass" for number in (1, 2, 3, 4, 5, 6, 7, 10, 12)}, {
        number: result.details for number, result in outcome.items() if result.status != "pass"
    }
    assert outcome[3].summary.startswith("71 requests compared")
    assert "(website product prices: the imported current LIST prices" in outcome[10].summary


def test_row_counts_fail_for_unmapped_rows_and_unaccounted_tables(imported, legacy_cms, corpus):
    tables = load_tables(CMS_FIXTURE)
    tables["faqs_faq"].append({**tables["faqs_faq"][0], "id": 9999})
    tables["mystery_table"] = [{"id": 1}]
    LegacyMap.objects.filter(source_table="catalog_tag", source_id="1").update(source_id="4242")
    check = results(verifier(legacy_cms, corpus, sources={"CMS": TablesSource(tables)}), 1)[1]
    assert check.status == "fail"
    assert any("faqs_faq: 1 of 101 rows neither mapped nor listed (9999)" in detail for detail in check.details)
    assert any("catalog_tag: 1 mapped source ids no longer exist in the source (4242)" in detail for detail in check.details)
    assert any("mystery_table" in detail for detail in check.details)


def test_rows_listed_by_the_import_count_as_accounted(imported, legacy_cms, corpus):
    LegacyMap.objects.filter(source_system="BACKEND", source_table="bom_catalogitem", source_id="2").delete()
    assert results(verifier(legacy_cms, corpus), 1)[1].status == "pass"  # bom_catalogitem 2 was listed (brand_near_match)
    LegacyMap.objects.filter(source_system="BACKEND", source_table="bom_catalogitem", source_id="3").delete()
    assert results(verifier(legacy_cms, corpus), 1)[1].status == "fail"


def test_integrity_finds_dangling_map_rows(imported, legacy_cms, corpus):
    LegacyMap.objects.create(source_system="CMS", source_table="faqs_faq", source_id="x", target_table="faqs_faq", target_id=987654321)
    check = results(verifier(legacy_cms, corpus), 2)[2]
    assert check.status == "fail" and check.details == ["core_legacy_map → faqs_faq: 1 dangling targets"]


def test_delivery_parity_reports_each_difference(imported, legacy_cms, corpus):
    status, body = legacy_cms.get("/api/page-content", {"route": "/subsidy"})
    legacy_cms.override("/api/page-content", {"route": "/subsidy"}, (status, {**body, "data": {**body["data"], "legacy_only": "x"}}))
    legacy_cms.override("/api/job-positions/crs-executive", {}, (404, None))
    check = results(verifier(legacy_cms, corpus), 3)[3]
    assert check.status == "fail" and len(check.details) == 2
    assert check.details[0] == "page-content /subsidy: $.data.legacy_only missing in the new payload"
    assert check.details[1] == "job-positions/crs-executive: status 404 → 200"


def test_delivery_parity_needs_the_legacy_api(imported, corpus):
    assert results(verifier(None, corpus), 3)[3].status == "skipped"


def test_media_checks_stored_checksums_and_heads(imported, legacy_cms, corpus, monkeypatch, settings):
    from migrations_tools.services import verify

    data = b"\x89PNG not really"
    (settings.PUBLIC_MEDIA_ROOT / "cms").mkdir(parents=True)
    (settings.PUBLIC_MEDIA_ROOT / "cms" / "a.png").write_bytes(data)
    good = MediaAsset.objects.create(
        visibility="PUBLIC",
        kind="IMAGE",
        file="cms/a.png",
        cdn_url="https://cdn.example/a.png",
        original_filename="a.png",
        mime_type="image/png",
        size_bytes=len(data),
        checksum_sha256=hashlib.sha256(data).hexdigest(),
    )
    LegacyMap.objects.create(source_system="CMS", source_table="media_asset", source_id="1", target_table="media_asset", target_id=good.pk)
    offline = results(verifier(legacy_cms, corpus), 4)[4]
    assert offline.status == "pass" and "1 stored files checksummed" in offline.summary
    MediaAsset.objects.filter(pk=good.pk).update(checksum_sha256="0" * 64)
    heads = []
    monkeypatch.setattr(verify, "head_status", lambda url: heads.append(url) or 404)
    online = verifier(legacy_cms, corpus)
    online.offline = False
    check = results(online, 4)[4]
    assert heads == ["https://cdn.example/a.png"] and check.status == "fail" and len(check.details) == 2


def test_slug_check_fails_for_an_alias_that_no_longer_resolves(imported, legacy_cms, corpus):
    from blog.models import EntrySlugHistory

    EntrySlugHistory.objects.update(active=False)
    check = results(verifier(legacy_cms, corpus), 5)[5]
    assert check.status == "fail" and check.details[0].startswith("alias net-metering-explained")


def test_users_need_a_role_no_usable_password_and_a_reset_link(imported, legacy_cms, corpus, django_capture_on_commit_callbacks):
    accounts_import.import_cms_users([{"id": 1, "username": "e", "email": "e@example.com", "role": "editor", "access_role_id": 2, "is_active": True, "date_joined": "2025-01-01T00:00:00+00:00"}])
    LegacyMap.objects.filter(source_table="accounts_admin_user").update(source_system="CMS")
    check = results(verifier(legacy_cms, corpus), 6)[6]
    assert check.status == "fail" and "no reset link issued" in check.details[0]
    user = User.objects.get(email="e@example.com")
    user.set_password("Legacy-Password-1")
    user.save()
    with django_capture_on_commit_callbacks(execute=True):
        accounts_import.issue_reset_links()
    check = results(verifier(legacy_cms, corpus), 6)[6]
    assert check.status == "fail" and check.details == ["e@example.com: carries a usable password it never set on the platform"]
    user.set_unusable_password()
    user.save()
    assert results(verifier(legacy_cms, corpus), 6)[6].status == "pass"


def test_pricing_detects_a_price_that_differs_from_the_source(imported, legacy_cms, corpus):
    component = Component.objects.get(pk=LegacyMap.objects.get(source_system="BACKEND", source_table="bom_catalogitem", source_id="1").target_id)
    tables = load_tables(BACKEND_FIXTURE)
    item = next(row for row in tables["bom_catalogitem"] if row["id"] == 1)
    item["price"], item["per_watt"] = "99.00", "1.0000"
    check = results(verifier(legacy_cms, corpus, sources={"BACKEND": TablesSource(tables)}), 7)[7]
    current = Price.objects.get(component=component, kind="LIST", effective_to__isnull=True)
    assert check.status == "fail" and check.details == [f"{component.sku}: LIST {current.amount} ≠ source 99.00", f"{component.sku}: per watt {current.per_watt} ≠ source 1.0000"]
    Price.objects.filter(pk=current.pk).update(effective_to=current.effective_from)  # closing is the only allowed change
    assert results(verifier(legacy_cms, corpus), 7)[7].details == [f"{component.sku}: no current LIST price (source 13122.00)"]


def test_calculators_without_a_release_name_the_cause(imported, legacy_cms, corpus):
    check = results(verifier(legacy_cms, corpus), 10)[10]
    assert check.summary.startswith("26 recorded requests replayed")
    assert check.status == "pass" or "no PriceRelease is published" in check.summary


def test_calculators_skip_without_a_corpus(imported, legacy_cms, tmp_path):
    check = results(verifier(legacy_cms, {"calculators": tmp_path / "none", "emi": tmp_path / "none"}), 10)[10]
    assert check.status == "skipped" and "corpus not found" in check.summary


def test_audit_detects_a_source_changed_since_the_import(imported, legacy_cms, corpus):
    tables = load_tables(CMS_FIXTURE)
    tables["faqs_faq"][0]["question"] = "Changed after the import?"
    check = results(verifier(legacy_cms, corpus, sources={"CMS": TablesSource(tables)}), 12)[12]
    assert check.status == "fail" and check.details == [check.details[0]] and "faqs_faq changed in the source" in check.details[0]


def test_audit_fails_for_a_step_never_imported(db, legacy_cms, corpus):
    check = results(verifier(legacy_cms, corpus, sources={"CMS": None}), 12)[12]
    assert check.status == "fail" and len(check.details) == 10


def test_checks_needing_a_source_are_skipped(db, legacy_cms, corpus):
    outcome = results(verifier(legacy_cms, corpus, sources={"CMS": None, "BACKEND": None}), 1, 3, 5, 7)
    assert {result.status for result in outcome.values()} == {"skipped"}


def test_a_crashing_check_is_a_failed_check(db, legacy_cms, corpus, monkeypatch):
    check = verifier(legacy_cms, corpus)
    monkeypatch.setattr(check, "integrity", lambda: 1 / 0)
    check.run = Verifier.run.__get__(check)
    assert results(check, 2)[2].status == "fail"


class TestCommand:
    def test_passes_on_fixtures_with_the_skips_allowed(self, imported):
        out = run("verify_migration", "--cms-fixture", str(CMS_FIXTURE), "--backend-fixture", str(BACKEND_FIXTURE), "--offline", "--check", "1", "--check", "2", "--check", "12")
        assert "verification passed" in out and "#1  PASS" in out

    def test_fails_on_a_skipped_check_unless_allowed(self, imported, tmp_path):
        with pytest.raises(CommandError, match="#3"):
            run("verify_migration", "--source", "cms", "--cms-fixture", str(CMS_FIXTURE), "--check", "3")
        report = tmp_path / "verify.json"
        out = run("verify_migration", "--source", "cms", "--cms-fixture", str(CMS_FIXTURE), "--check", "3", "--allow-skipped", "--json", str(report))
        assert "SKIPPED" in out and json.loads(report.read_text())[0]["status"] == "skipped"

    def test_bad_source_url(self, db):
        with pytest.raises(CommandError, match="postgresql://"):
            run("verify_migration", "--cms-url", "http://x")


def test_first_difference_and_typed_comparison():
    assert parity.first_difference({"a": [1, {"b": 1}]}, {"a": [1, {"b": 1.0}]}) is not None
    assert parity.first_difference({"a": 1}, {"a": 1, "b": 2}) == "$.b only in the new payload"
    assert parity.first_difference([1], [1, 2]).endswith("1 items in the legacy payload, 2 in the new")
    assert parity.compare_calculator({"status": 500, "response": None}, 400, {"code": "invalid_input"}) is None
    assert parity.compare_calculator({"status": 404, "response": {"error": "x size_id"}}, 404, {"message": "x size_uid"}, message=parity.EmiTranslation.message) is None
