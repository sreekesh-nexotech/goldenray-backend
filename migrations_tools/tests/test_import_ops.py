"""``import_pa`` → ``import_si`` (PLAN §7.5, in that order) and ``import_essl`` on the committed fixtures: counts,
cross-source links, idempotency, the command options, and ``verify_migration`` #1, #2, #11, #12."""

from __future__ import annotations

import json
import os
import stat

import pytest
from django.core.management.base import CommandError

from accounts.services.seeds import seed_roles
from agreements.models import Agreement
from attendance.models import AttendanceDay
from core.models import LegacyMap
from devices.models import Agent
from migrations_tools.services import pa
from migrations_tools.tests.conftest import run
from migrations_tools.tests.ops_fixtures import ESSL_CAPTURED, ESSL_SEEDED, PA_ADMIN, PA_CATALOG, PA_CRS, SI_DB, write_fixture
from site_inspections.models import Inspection
from site_inspections.services.legacy_import import agreement_uid

pytestmark = pytest.mark.django_db
PA_ARGS = ("--export", f"crs={PA_CRS}", "--export", f"admin={PA_ADMIN}", "--catalog", str(PA_CATALOG))


class TestPurchaseAgreementsThenSiteInspections:
    def test_import_pa_then_si_links_the_inspection_and_the_customer(self, tmp_path):
        seed_roles()
        report = tmp_path / "pa.json"
        out = run("import_pa", *PA_ARGS, "--json", str(report))
        data = json.loads(report.read_text())
        results = {label: result for step in data["steps"] for label, result in step["results"].items()}
        assert results["kseb"]["created"] == 6
        assert results["flarize_agr:crs"]["created"] == 7
        # the admin profile holds the page's demo records (never migrated) and a record id the crs profile holds too
        assert results["flarize_agr:admin"]["created"] == 1
        assert {"demo_record_not_migrated", "duplicate_record_id"} <= {v["code"] for v in results["flarize_agr:admin"]["violations"]}
        assert [v["code"] for v in results["flarize_trash:admin"]["violations"]] == ["trashed_record_not_migrated"]
        assert {v["code"] for v in results["catalog"]["violations"]} == {"not_in_catalog", "listed_only"}
        assert "pa.agreements_admin [imported]" in out
        # the agreement uid is the site-inspection key of its record id; the second copy of a record id gets its own
        agreement = Agreement.objects.get(legacy_ref="crs/agr_1789902000000")
        assert agreement.uid == agreement_uid("agr_1789902000000")
        assert Agreement.objects.get(legacy_ref="crs/agr_1789909200000").uid == agreement_uid("agr_1789909200000")
        assert Agreement.objects.get(legacy_ref="admin/agr_1789909200000").uid != agreement_uid("agr_1789909200000")
        assert "totals: created 0, updated 0" in run("import_pa", *PA_ARGS)
        si = run("import_si", "--source-file", str(SI_DB))
        assert "si.inspections [imported]" in si and "agreement links                    created     0  updated     0  unchanged     1  violations    0" in si
        inspection = Inspection.objects.get(agreement_uid=agreement.uid)
        assert inspection.customer_id == agreement.customer_id  # matched by phone across the two sources
        assert "totals: created 0, updated 0" in run("import_si", "--source-file", str(SI_DB))
        out = run(
            "verify_migration", "--source", "pa", *[arg for pair in (("--pa-export", f"crs={PA_CRS}"), ("--pa-export", f"admin={PA_ADMIN}")) for arg in pair], "--pa-catalog", str(PA_CATALOG),
            "--source", "si", "--si-file", str(SI_DB), "--offline",
        )  # fmt: skip
        assert "#1  PASS" in out and "#12 PASS" in out and "verification passed" in out

    def test_si_before_pa_lists_the_unlinked_inspection(self):
        seed_roles()
        out = run("import_si", "--source-file", str(SI_DB))
        assert "agreement_not_imported" in out and "run import_pa first" in out

    def test_pa_options(self, tmp_path):
        seed_roles()
        with pytest.raises(CommandError, match="--export PROFILE=FILE"):
            run("import_pa")
        with pytest.raises(CommandError, match="PROFILE one of crs / admin|PROFILE one of crs, admin"):
            run("import_pa", "--export", f"guest={PA_CRS}")
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"something": 1}))
        with pytest.raises(CommandError, match="flarize_agr"):
            run("import_pa", "--export", f"crs={bad}")
        out = run("import_pa", "--export", f"crs={PA_CRS}", "--dry-run")
        assert "no export for the 'admin' profile" in out and "DRY RUN" in out and not Agreement.objects.exists()
        # without the Upstash catalog the page's built-in KSEB fees are imported
        run("import_pa", "--export", f"crs={PA_CRS}", "--only", "pa.kseb_fees")
        assert LegacyMap.objects.filter(source_system="PA", source_table="kseb").count() == len(pa.PAGE_KSEB_FEES)

    def test_verify_row_counts_fail_on_a_record_saved_after_the_import(self, tmp_path):
        seed_roles()
        run("import_pa", *PA_ARGS)
        records = json.loads(PA_CRS.read_text())
        later = tmp_path / "crs.json"
        later.write_text(json.dumps([*records, dict(records[0], id="agr_1789999999999")]))
        with pytest.raises(CommandError, match="#1"):
            run("verify_migration", "--source", "pa", "--pa-export", f"crs={later}", "--pa-export", f"admin={PA_ADMIN}", "--pa-catalog", str(PA_CATALOG), "--check", "1")

    def test_si_source_errors(self, tmp_path):
        with pytest.raises(CommandError, match="--source-file"):
            run("import_si")
        with pytest.raises(CommandError, match="no such SQLite file"):
            run("import_si", "--source-file", str(tmp_path / "none.db"))


class TestEssl:
    def test_captured_essl_imports_recomputes_and_verifies_with_hr_signoff(self, tmp_path):
        seed_roles()
        diff_path, credentials = tmp_path / "diff.json", tmp_path / "creds.json"
        out = run("import_essl", "--source-fixture", str(ESSL_CAPTURED), "--diff-report", str(diff_path), "--credentials-file", str(credentials))
        assert "B-7 — attendance v3 → v4: 5 of 155 employee-days differ" in out and "35 eSSL rows → 34 new punches, 1 duplicates collapsed" in out
        assert stat.S_IMODE(os.stat(credentials).st_mode) == 0o600 and json.loads(credentials.read_text())[0]["agent_code"] == Agent.objects.get().code
        assert json.loads(diff_path.read_text())["days_differing"] == 5 and AttendanceDay.objects.exists()
        again = run("import_essl", "--source-fixture", str(ESSL_CAPTURED), "--diff-report", str(diff_path), "--credentials-file", str(credentials))
        assert "totals: created 0, updated 0" in again and "new agent token" not in again
        report = tmp_path / "hr.json"
        verify = ("verify_migration", "--source", "essl", "--essl-fixture", str(ESSL_CAPTURED), "--offline", "--attendance-report", str(report))
        with pytest.raises(CommandError, match="#11"):
            run(*verify)
        digest = json.loads(report.read_text())["sha256"]
        out = run(*verify, "--attendance-signoff", digest)
        assert "#11 PASS" in out and "34 punches (1 duplicates collapsed" in out and "signed off by HR" in out and "#1  PASS" in out
        # a diff that changed after the sign-off is not signed off
        AttendanceDay.all_objects.filter(pk=AttendanceDay.all_objects.order_by("pk").values("pk")[:1]).update(status="PRESENT")
        with pytest.raises(CommandError, match="#11"):
            run(*verify, "--attendance-signoff", digest)

    def test_seeded_essl_and_raw_accounting_failure(self, tmp_path):
        seed_roles()
        diff = ("--diff-report", str(tmp_path / "diff.json"), "--credentials-file", str(tmp_path / "creds.json"))
        out = run("import_essl", "--source-fixture", str(ESSL_SEEDED), *diff)
        assert "essl.hr [imported]" in out and "offices                            created     3" in out and "0 of 0 employee-days differ" in out
        tables = json.loads(ESSL_CAPTURED.read_text())
        source = write_fixture(tmp_path / "essl.json", tables.get("tables", tables))
        run("import_essl", "--source-fixture", str(source), *diff)
        LegacyMap.objects.filter(source_system="ESSL", source_table="attendance_raw").order_by("pk").first().delete()
        with pytest.raises(CommandError, match="#11"):
            run("verify_migration", "--source", "essl", "--essl-fixture", str(source), "--check", "11", "--allow-skipped")

    def test_dry_run_issues_no_credential_file(self, tmp_path, monkeypatch):
        seed_roles()
        monkeypatch.chdir(tmp_path)
        out = run("import_essl", "--source-fixture", str(ESSL_CAPTURED), "--dry-run")
        assert "DRY RUN" in out and not list(tmp_path.glob("essl-agent-credentials-*.json")) and not Agent.objects.exists()
        assert list(tmp_path.glob("essl-attendance-diff-*.json"))  # the diff of a dry run is still handed over


def test_si_engineers_get_reset_links_once_their_placeholder_address_is_replaced():
    """PLAN §7.5: SI engineers → Field Engineer users with a forced reset. A placeholder (``.invalid``) address is never
    mailed; once staff replaced it, ``import_si --send-reset-links`` issues the link and #6 covers SI accounts."""
    from accounts.models import PasswordReset, User

    seed_roles()
    out = run("import_si", "--source-file", str(SI_DB), "--send-reset-links")
    engineers = User.objects.filter(pk__in=LegacyMap.objects.filter(source_system="SI", source_table="engineers").values("target_id"))
    assert engineers.exists() and all(account.email.endswith(".invalid") for account in engineers)
    assert "reset links: 0 sent" in out and not PasswordReset.objects.filter(user__in=engineers).exists()
    with pytest.raises(CommandError, match="#6"):
        User.objects.filter(pk=engineers[0].pk).update(email="engineer.one@example.com")
        run("verify_migration", "--source", "si", "--si-file", str(SI_DB), "--check", "6")
    out = run("import_si", "--source-file", str(SI_DB), "--send-reset-links")
    assert "reset links: 1 sent" in out and PasswordReset.objects.filter(user=engineers[0]).count() == 1
    assert "#6  PASS" in run("verify_migration", "--source", "si", "--si-file", str(SI_DB), "--check", "6")


def test_essl_row_keys_keep_adms_evidence_imported_before_it_aged_out():
    """#1 must not call ADMS evidence imported inside the retention window an orphan once it is older than the window
    (verify runs days after the cutover import)."""
    from datetime import timedelta

    from django.utils import timezone

    from migrations_tools.services import essl

    now = timezone.now()
    rows = [
        {"id": 1, "received_at": (now - timedelta(days=40)).isoformat()},
        {"id": 2, "received_at": (now - timedelta(days=1)).isoformat()},
        {"id": 3, "received_at": (now - timedelta(days=90)).isoformat()},
    ]
    LegacyMap.objects.create(source_system="ESSL", source_table="adms_requests", source_id="1", target_table="devices_adms_request", target_id=1)
    assert sorted(essl.PLAN.keys_of("adms_requests", rows)) == [("adms_requests", "1"), ("adms_requests", "2")]
