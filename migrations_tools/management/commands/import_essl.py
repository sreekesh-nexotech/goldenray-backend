"""Import the eSSL attendance database (PLAN §7.5): users, HR masters, devices/agents, raw punches, the v4 recompute
and the v3/v4 status diff for HR. Run ``seed_roles`` first; freeze eSSL (agents queue locally) at C6."""

import json
import os
from pathlib import Path

from django.core.serializers.json import DjangoJSONEncoder

from migrations_tools.services.cli import ImportCommand
from migrations_tools.services.essl import PLAN


def write_private(path: str | Path, payload) -> Path:
    """JSON with mode 0600 (also when the file already existed): tokens and personal data."""
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, cls=DjangoJSONEncoder, indent=1, ensure_ascii=False)
    return path


class Command(ImportCommand):
    help = "Import the eSSL database (read-only source): users, offices, shifts, employees, leave, devices, agents, raw punches; recompute v4 and report the v3/v4 diff, idempotently."
    plan = PLAN
    source_name = "eSSL"
    website_options = False

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--diff-report", help="where to write the v3/v4 per-employee-month status diff for HR (default: essl-attendance-diff-<run uid>.json)")
        parser.add_argument("--credentials-file", help="where to write the new agent tokens, shown once (default: essl-agent-credentials-<run uid>.json, mode 0600)")

    def after_run(self, report, context, options) -> None:
        artefacts = context.options.get("artefacts") or {}
        diff = artefacts.get("attendance_diff")
        if diff is not None:
            path = write_private(options["diff_report"] or f"essl-attendance-diff-{report.run_uid}.json", {"run_uid": report.run_uid, "dry_run": report.dry_run, **diff})
            self.stdout.write(self.style.MIGRATE_HEADING(f"B-7 — attendance v3 → v4: {diff['days_differing']} of {diff['days_compared']} employee-days differ; the diff for HR sign-off is in {path}"))
            for status, counts in diff["month_totals"].items():
                self.stdout.write(f"  {status:14} v3 {counts['v3']:6}  v4 {counts['v4']:6}")
            raw = diff["raw_punches"]
            self.stdout.write(f"  raw punches: {raw['source_rows']} eSSL rows → {raw['created']} new punches, {raw['collapsed']} duplicates collapsed")
        credentials = artefacts.get("agent_credentials") or []
        if credentials and not report.dry_run:
            path = write_private(options["credentials_file"] or f"essl-agent-credentials-{report.run_uid}.json", credentials)
            self.stdout.write(self.style.WARNING(f"{len(credentials)} new agent token(s) written to {path} (mode 0600, shown once): reconfigure the office agents, then delete the file."))
