"""Shared implementation of the ``import_cms`` / ``import_backend`` management commands."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from migrations_tools.services.runner import Context, FileReader, Plan, RunReport, batch_rows, latest_run, run_plan
from migrations_tools.services.source import SourceError, open_source

MAX_PRINTED_VIOLATIONS = 40


def resolve_actor(email: str | None):
    if not email:
        return None
    from accounts.models import User

    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user is None:
        raise CommandError(f"--actor {email}: no active platform user with that e-mail.")
    return user


def report_as_dict(report: RunReport) -> dict:
    return {
        "run_uid": report.run_uid,
        "source_system": report.source_system,
        "source": report.source,
        "dry_run": report.dry_run,
        "ok": report.ok,
        "totals": report.totals(),
        "steps": [
            {"step": step.step, "phase": step.phase, "status": step.status, "checksum": step.checksum, "tables": step.tables, "results": step.results, "error": step.error} for step in report.steps
        ],
    }


class ImportCommand(BaseCommand):
    plan: Plan
    source_name = ""

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group()
        source.add_argument("--source-url", help=f"postgresql:// URL of the legacy {self.source_name} database (opened read-only)")
        source.add_argument("--source-fixture", help="JSON file {table: [rows]} instead of a database (rehearsals from exported fixtures, tests)")
        parser.add_argument("--dry-run", action="store_true", help="print counts and violations; write nothing")
        parser.add_argument("--verify", action="store_true", help="run verify_migration for this source afterwards")
        parser.add_argument("--resume", nargs="?", const="latest", help="continue a run: its uid, or 'latest'; steps already imported with the same source checksum are skipped")
        parser.add_argument("--only", action="append", default=[], help="run only this step (repeatable; see --list-steps)")
        parser.add_argument("--list-steps", action="store_true", help="print the plan and exit")
        parser.add_argument("--media-root", help="the legacy media volume (uploads, resumes); files are read from here")
        parser.add_argument("--actor", help="e-mail of the platform user the import is attributed to (default: SYSTEM)")
        parser.add_argument("--json", dest="json_path", help="write the full report (every violation) to this file")
        parser.add_argument("--send-reset-links", action="store_true", help="after the import, e-mail a set-password link to every migrated account that needs one (cutover)")
        parser.add_argument("--site-url", help="the legacy site URL (CMS FRONTEND_BASE_URL) for company_profile.website when empty (default: FRONTEND_BASE_URL)")
        parser.add_argument("--legacy-api-url", help="--verify: base URL of the legacy HTTP API for delivery parity")
        parser.add_argument("--offline", action="store_true", help="--verify: skip the media HEAD checks")

    def handle(self, *args, **options):
        if options["list_steps"]:
            for step in self.plan.steps:
                self.stdout.write(f"{step.name:28} {step.phase:14} {', '.join(step.tables)} — {step.description}")
            for table, reason in self.plan.not_migrated.items():
                self.stdout.write(f"{'(not migrated)':28} {table}: {reason}")
            return
        if not options["source_url"] and not options["source_fixture"]:
            raise CommandError("one of --source-url / --source-fixture is required.")
        if options["verify"] and options["dry_run"]:
            raise CommandError("--verify cannot follow a --dry-run: nothing was imported to verify.")
        unknown = set(options["only"]) - {step.name for step in self.plan.steps}
        if unknown:
            raise CommandError(f"unknown step(s): {', '.join(sorted(unknown))}")
        resume = options["resume"]
        if resume == "latest":
            resume = latest_run(self.plan.source_system)
            if resume is None:
                raise CommandError("--resume latest: no earlier run of this source.")
        elif resume is not None:
            try:
                resume = str(uuid.UUID(resume))
            except ValueError as exc:
                raise CommandError(f"--resume {resume}: not a run uid (see the 'run uid:' line of the run to continue).") from exc
            if not batch_rows(resume).filter(after__source_system=self.plan.source_system).exists():
                raise CommandError(f"--resume {resume}: no {self.plan.source_system} import run with that uid.")
        context = Context(
            source_system=self.plan.source_system,
            user=resolve_actor(options["actor"]),
            dry_run=options["dry_run"],
            read_file=FileReader(options["media_root"]) if options["media_root"] else None,
            options={"site_url": options["site_url"]},
        )
        try:
            source = open_source(options["source_url"], fixture=options["source_fixture"])
        except SourceError as exc:
            raise CommandError(str(exc)) from exc
        with source:
            report = run_plan(self.plan, source, context=context, resume=resume, only=set(options["only"]) or None, log=lambda message: self.stdout.write(f"  {message}"))
        self._print(report)
        if options["json_path"]:
            Path(options["json_path"]).write_text(json.dumps(report_as_dict(report), cls=DjangoJSONEncoder, indent=1, ensure_ascii=False), encoding="utf-8")
        if not report.ok:
            raise CommandError(f"import stopped at a failed step; fix it and run again with --resume {report.run_uid}")
        if options["send_reset_links"] and not context.dry_run:
            from accounts.services.legacy_import import issue_reset_links

            links = issue_reset_links(user=context.user)
            self.stdout.write(f"reset links: {links['created']} sent, {links['skipped']} not needed or not sendable ({len(links['violations'])} listed)")
        if options["verify"]:
            system = self.plan.source_system.lower()
            verify_options = {"source": [system], "offline": options["offline"]}
            if options["source_url"]:
                verify_options[f"{system}_url"] = options["source_url"]
            if options["source_fixture"]:
                verify_options[f"{system}_fixture"] = options["source_fixture"]
            if options["legacy_api_url"]:
                verify_options["legacy_api_url"] = options["legacy_api_url"]
            call_command("verify_migration", stdout=self.stdout, **verify_options)

    def _print(self, report: RunReport) -> None:
        mode = "DRY RUN (rolled back)" if report.dry_run else "committed"
        self.stdout.write(self.style.MIGRATE_HEADING(f"{report.source_system} import {report.run_uid} from {report.source} — {mode}"))
        violations = []
        for step in report.steps:
            self.stdout.write(f"{step.step} [{step.status}]" + (f" {step.error}" if step.error else ""))
            for label, result in step.results.items():
                self.stdout.write(f"    {label:34} created {result['created']:5}  updated {result['updated']:5}  unchanged {result['unchanged']:5}  violations {len(result['violations']):4}")
                violations.extend((label, violation) for violation in result["violations"])
        totals = report.totals()
        self.stdout.write(f"totals: created {totals.get('created', 0)}, updated {totals.get('updated', 0)}, unchanged {totals.get('unchanged', 0)}, violations {totals.get('violations', 0)}")
        for label, violation in violations[:MAX_PRINTED_VIOLATIONS]:
            self.stdout.write(f"  ! {label} {violation.get('source_id', '')} {violation.get('code') or violation.get('field', '')}: {violation.get('message', '')}")
        if len(violations) > MAX_PRINTED_VIOLATIONS:
            self.stdout.write(f"  … {len(violations) - MAX_PRINTED_VIOLATIONS} more (use --json)")
        self.stdout.write(f"run uid: {report.run_uid}")
