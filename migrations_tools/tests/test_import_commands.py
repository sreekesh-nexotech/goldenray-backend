"""``import_cms`` / ``import_backend`` on the committed legacy exports: counts, idempotency, dry run, batches, resume."""

import json

import pytest
from django.core.management.base import CommandError

from accounts.models import Role, User
from accounts.services.seeds import seed_roles
from audit.models import AuditLog
from blog.models import Entry
from core.models import LegacyMap
from faqs.models import Faq
from migrations_tools.services import backend, cms, runner
from migrations_tools.services.runner import BATCH_ACTION, Plan, Step
from migrations_tools.tests.conftest import BACKEND_FIXTURE, CMS_FIXTURE, load_tables, run
from sitepages.models import Page

pytestmark = pytest.mark.django_db
CMS_TABLES = load_tables(CMS_FIXTURE)
BACKEND_TABLES = load_tables(BACKEND_FIXTURE)


def batches():
    return AuditLog.objects.filter(action=BATCH_ACTION).order_by("at", "id")


class TestFullImport:
    def test_every_table_is_imported_in_fk_order(self, imported):
        cms_out, backend_out = imported
        assert "totals: created 231, updated 1" in cms_out and "totals: created 6062" not in backend_out  # pincodes are trimmed in the fixture
        assert Entry.objects.count() == 7 and Page.objects.count() == 27 and Faq.objects.count() == 100
        assert Role.objects.filter(slug__in=["careers-hr", "sales-lead"]).count() == 2
        for table in cms.PLAN.tables + backend.PLAN.tables:
            rows = (CMS_TABLES if table in cms.PLAN.tables else BACKEND_TABLES).get(table, [])
            system = "CMS" if table in cms.PLAN.tables else "BACKEND"
            mapped = {value.split(":", 1)[0] for value in LegacyMap.objects.filter(source_system=system, source_table=table).values_list("source_id", flat=True)}
            assert {str(row["id"]) for row in rows} <= mapped, table
        steps = [batch.after["step"] for batch in batches()]
        assert steps == [step.name for step in cms.PLAN.steps] + [step.name for step in backend.PLAN.steps]

    def test_a_second_run_changes_nothing(self, imported):
        before = LegacyMap.objects.count()
        cms_out = run("import_cms", "--source-fixture", str(CMS_FIXTURE))
        backend_out = run("import_backend", "--source-fixture", str(BACKEND_FIXTURE))
        assert "totals: created 0, updated 0" in cms_out and "totals: created 0, updated 0" in backend_out
        assert LegacyMap.objects.count() == before

    def test_batch_rows_carry_counts_checksums_and_listed_ids(self, imported):
        batch = batches().get(after__step="backend.catalog_pricing")
        after = batch.after
        assert after["source_system"] == "BACKEND" and after["phase"] == "masters" and len(after["checksum"]) == 64
        assert after["tables"]["bom_catalogitem"]["rows"] == len(BACKEND_TABLES["bom_catalogitem"])
        assert after["counts"]["pricing.prices"]["created"] == 261
        assert after["violation_codes"] == {"brand_near_match": 3} and set(after["listed"]["bom_catalogitem"]) == {"2", "11", "30"}
        assert batch.object_type == "migrations_tools.importrun" and batch.actor_kind == "SYSTEM"


class TestOptions:
    def test_dry_run_writes_nothing(self, db):
        seed_roles()
        audit_before = AuditLog.objects.count()
        out = run("import_cms", "--source-fixture", str(CMS_FIXTURE), "--dry-run")
        assert "DRY RUN (rolled back)" in out and "totals: created 231" in out
        assert not LegacyMap.objects.exists() and not Entry.all_objects.exists() and AuditLog.objects.count() == audit_before

    def test_list_steps(self, db):
        out = run("import_backend", "--list-steps")
        assert "backend.catalog_pricing" in out and "(not migrated)" in out and "sent_quotes" in out

    def test_only_one_step_and_unknown_steps(self, db):
        seed_roles()
        out = run("import_cms", "--source-fixture", str(CMS_FIXTURE), "--only", "cms.users")
        assert "cms.users [imported]" in out and "cms.media" not in out
        with pytest.raises(CommandError, match="unknown step"):
            run("import_cms", "--source-fixture", str(CMS_FIXTURE), "--only", "cms.nope")

    def test_actor_attribution(self, db):
        seed_roles()
        actor = User.objects.create_user("ops@example.com", role=Role.objects.get(slug="admin"))
        run("import_cms", "--source-fixture", str(CMS_FIXTURE), "--only", "cms.users", "--actor", "OPS@example.com")
        assert batches().get().actor_id == actor.pk
        with pytest.raises(CommandError, match="no active platform user"):
            run("import_cms", "--source-fixture", str(CMS_FIXTURE), "--actor", "ghost@example.com")

    def test_bad_source_and_missing_resume(self, db):
        with pytest.raises(CommandError, match="postgresql://"):
            run("import_cms", "--source-url", "mysql://x")
        with pytest.raises(CommandError, match="cannot connect"):
            run("import_cms", "--source-url", "postgresql://nobody:wrong@127.0.0.1:1/none")
        with pytest.raises(CommandError, match="no earlier run"):
            run("import_cms", "--source-fixture", str(CMS_FIXTURE), "--resume")

    def test_resume_needs_a_run_of_this_source(self, db):
        seed_roles()
        with pytest.raises(CommandError, match="not a run uid"):
            run("import_cms", "--source-fixture", str(CMS_FIXTURE), "--resume", "yesterday")
        with pytest.raises(CommandError, match="no CMS import run"):
            run("import_cms", "--source-fixture", str(CMS_FIXTURE), "--resume", "5c1f3a52-0000-4000-8000-000000000000")
        run("import_backend", "--source-fixture", str(BACKEND_FIXTURE), "--only", "backend.users")
        backend_run = str(batches().get().object_uid)
        with pytest.raises(CommandError, match="no CMS import run"):
            run("import_cms", "--source-fixture", str(CMS_FIXTURE), "--resume", backend_run)
        assert batches().count() == 1

    def test_json_report_and_media_root(self, db, tmp_path):
        seed_roles()
        report = tmp_path / "report.json"
        (tmp_path / "volume").mkdir()
        run("import_backend", "--source-fixture", str(BACKEND_FIXTURE), "--only", "backend.company", "--json", str(report), "--media-root", str(tmp_path / "volume"))
        data = json.loads(report.read_text())
        assert data["ok"] and data["steps"][0]["step"] == "backend.company" and data["steps"][0]["results"]["bom_quotationsettings"]["created"] == 1


class TestResume:
    def test_failed_step_stops_the_run_and_resume_skips_what_was_imported(self, db, monkeypatch):
        seed_roles()
        calls = []

        def broken(rows, ctx):
            calls.append("broken")
            raise RuntimeError("boom")

        from migrations_tools.management.commands import import_cms

        steps = list(cms.PLAN.steps)
        steps[2] = Step(steps[2].name, steps[2].phase, steps[2].tables, broken)
        monkeypatch.setattr(import_cms.Command, "plan", Plan(cms.PLAN.source_system, tuple(steps), cms.PLAN.not_migrated))
        with pytest.raises(CommandError, match="--resume") as failure:
            run("import_cms", "--source-fixture", str(CMS_FIXTURE))
        run_uid = str(failure.value).rsplit(" ", 1)[1]
        assert calls == ["broken"]
        assert [batch.after["step"] for batch in batches()] == ["cms.users", "cms.media"]  # committed before the failure
        assert Role.objects.filter(slug="sales-lead").exists() and not Entry.all_objects.exists()

        monkeypatch.setattr(import_cms.Command, "plan", cms.PLAN)
        out = run("import_cms", "--source-fixture", str(CMS_FIXTURE), "--resume", run_uid)
        assert "cms.users [resumed]" in out and "cms.media [resumed]" in out and "cms.blog_schema [imported]" in out
        assert batches().filter(object_uid=run_uid).count() == len(cms.PLAN.steps)
        latest = run("import_cms", "--source-fixture", str(CMS_FIXTURE), "--resume")
        assert "[imported]" not in latest and latest.count("[resumed]") == len(cms.PLAN.steps)


def test_plans_must_follow_the_fk_order():
    with pytest.raises(ValueError, match="FK order"):
        Plan("CMS", (Step("b", "content", (), lambda rows, ctx: {}), Step("a", "users", (), lambda rows, ctx: {})))


def test_every_source_table_is_imported_or_listed():
    for plan, tables in ((cms.PLAN, CMS_TABLES), (backend.PLAN, BACKEND_TABLES)):
        assert set(tables) <= set(plan.tables) | set(plan.not_migrated)


def test_file_reader_stays_inside_its_root(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "a.txt").write_bytes(b"ok")
    (tmp_path / "secret.txt").write_bytes(b"no")
    reader = runner.FileReader(tmp_path / "root")
    assert reader("a.txt") == b"ok" and reader("/a.txt") == b"ok"
    assert reader("../secret.txt") is None and reader("missing.txt") is None


def test_normalise_merges_skipped_and_unchanged():
    assert runner.normalise({"created": 1, "skipped": 2, "unchanged": 3, "violations": None}) == {"created": 1, "updated": 0, "unchanged": 5, "violations": []}
