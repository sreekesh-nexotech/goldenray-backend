"""Shared implementation of the ``import_*`` management commands (website sources, Flarize, PA, SI, eSSL).

A command names its :class:`~migrations_tools.services.runner.Plan` and how its source is given
(:meth:`ImportCommand.add_source_arguments` / :meth:`ImportCommand.open_source`); everything else — dry run, resume,
``--only``, the report (with the business-default lists), ``--json``, reset links and ``--verify`` — is common.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder

from migrations_tools.services import business_defaults
from migrations_tools.services.runner import Context, FileReader, Plan, RunReport, batch_rows, latest_run, run_plan
from migrations_tools.services.source import Source, SourceError, open_source

MAX_PRINTED_VIOLATIONS = 40


def resolve_actor(email: str | None):
    if not email:
        return None
    from accounts.models import User

    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user is None:
        raise CommandError(f"--actor {email}: no active platform user with that e-mail.")
    return user


def labelled_violations(report: RunReport) -> list[tuple[str, dict]]:
    return [(label, violation) for step in report.steps for label, result in step.results.items() for violation in result["violations"]]


def report_as_dict(report: RunReport) -> dict:
    return {
        "run_uid": report.run_uid,
        "source_system": report.source_system,
        "source": report.source,
        "dry_run": report.dry_run,
        "ok": report.ok,
        "totals": report.totals(),
        "business_defaults": business_defaults.group(labelled_violations(report)),
        "steps": [
            {"step": step.step, "phase": step.phase, "status": step.status, "checksum": step.checksum, "tables": step.tables, "results": step.results, "error": step.error} for step in report.steps
        ],
    }


class ImportCommand(BaseCommand):
    plan: Plan
    source_name = ""
    verify_source = ""  # the verify_migration --source name (default: the plan's source system, lower-cased)
    website_options = True  # --site-url, --legacy-api-url, --offline (the CMS/main-backend verification)

    # ── source ─────────────────────────────────────────────────────────────────────────────────────────────────────
    def add_source_arguments(self, parser) -> None:
        source = parser.add_mutually_exclusive_group()
        source.add_argument("--source-url", help=f"postgresql:// URL of the legacy {self.source_name} database (opened read-only)")
        source.add_argument("--source-fixture", help="JSON file {table: [rows]} instead of a database (rehearsals from exported fixtures, tests)")

    def source_given(self, options) -> bool:
        return bool(options.get("source_url") or options.get("source_fixture"))

    def missing_source_message(self) -> str:
        return "one of --source-url / --source-fixture is required."

    def open_source(self, options) -> Source:
        return open_source(options["source_url"], fixture=options["source_fixture"])

    def verify_arguments(self, options) -> dict:
        """The ``verify_migration`` options naming this run's source."""
        system = self.verify_source or self.plan.source_system.lower()
        arguments = {}
        if options.get("source_url"):
            arguments[f"{system}_url"] = options["source_url"]
        if options.get("source_fixture"):
            arguments[f"{system}_fixture"] = options["source_fixture"]
        return arguments

    def context_options(self, options) -> dict:
        return {"site_url": options.get("site_url")}

    def after_run(self, report: RunReport, context: Context, options) -> None:
        """Hand over what the steps left in ``context.options["artefacts"]`` (runs even when a later step failed)."""

    # ── command ────────────────────────────────────────────────────────────────────────────────────────────────────
    def add_arguments(self, parser):
        self.add_source_arguments(parser)
        parser.add_argument("--dry-run", action="store_true", help="print counts and violations; write nothing")
        parser.add_argument("--verify", action="store_true", help="run verify_migration for this source afterwards")
        parser.add_argument("--resume", nargs="?", const="latest", help="continue a run: its uid, or 'latest'; steps already imported with the same source checksum are skipped")
        parser.add_argument("--only", action="append", default=[], help="run only this step (repeatable; see --list-steps)")
        parser.add_argument("--list-steps", action="store_true", help="print the plan and exit")
        parser.add_argument("--media-root", help="the legacy media volume (uploads, resumes); files are read from here")
        parser.add_argument("--actor", help="e-mail of the platform user the import is attributed to (default: SYSTEM)")
        parser.add_argument("--json", dest="json_path", help="write the full report (every violation) to this file")
        parser.add_argument("--send-reset-links", action="store_true", help="after the import, e-mail a set-password link to every migrated account that needs one (cutover)")
        if self.website_options:
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
        if not self.source_given(options):
            raise CommandError(self.missing_source_message())
        if options["verify"] and options["dry_run"]:
            raise CommandError("--verify cannot follow a --dry-run: nothing was imported to verify.")
        unknown = set(options["only"]) - {step.name for step in self.plan.steps}
        if unknown:
            raise CommandError(f"unknown step(s): {', '.join(sorted(unknown))}")
        resume = self._resume(options["resume"])
        user = resolve_actor(options["actor"])
        try:
            source = self.open_source(options)
        except SourceError as exc:
            raise CommandError(str(exc)) from exc
        read_file = FileReader(options["media_root"]) if options["media_root"] else getattr(source, "read_file", None)
        context = Context(source_system=self.plan.source_system, user=user, dry_run=options["dry_run"], read_file=read_file, options=self.context_options(options))
        with source:
            report = run_plan(self.plan, source, context=context, resume=resume, only=set(options["only"]) or None, log=lambda message: self.stdout.write(f"  {message}"))
        self._print(report)
        if options["json_path"]:
            Path(options["json_path"]).write_text(json.dumps(report_as_dict(report), cls=DjangoJSONEncoder, indent=1, ensure_ascii=False), encoding="utf-8")
        self.after_run(report, context, options)
        if not report.ok:
            raise CommandError(f"import stopped at a failed step; fix it and run again with --resume {report.run_uid}")
        if options["send_reset_links"] and not context.dry_run:
            from accounts.services.legacy_import import issue_reset_links

            links = issue_reset_links(user=context.user)
            self.stdout.write(f"reset links: {links['created']} sent, {links['skipped']} not needed or not sendable ({len(links['violations'])} listed)")
        if options["verify"]:
            verify_options = {"source": [self.verify_source or self.plan.source_system.lower()], **self.verify_arguments(options)}
            if self.website_options:
                verify_options["offline"] = options["offline"]
                if options["legacy_api_url"]:
                    verify_options["legacy_api_url"] = options["legacy_api_url"]
            call_command("verify_migration", stdout=self.stdout, **verify_options)

    def _resume(self, resume):
        if resume == "latest":
            resume = latest_run(self.plan.source_system)
            if resume is None:
                raise CommandError("--resume latest: no earlier run of this source.")
            return resume
        if resume is None:
            return None
        try:
            resume = str(uuid.UUID(resume))
        except ValueError as exc:
            raise CommandError(f"--resume {resume}: not a run uid (see the 'run uid:' line of the run to continue).") from exc
        if not batch_rows(resume).filter(after__source_system=self.plan.source_system).exists():
            raise CommandError(f"--resume {resume}: no {self.plan.source_system} import run with that uid.")
        return resume

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
        for key, rows in business_defaults.group(violations).items():
            # every row, never capped: these lists go to the business (docs/decisions/business-defaults.md)
            self.stdout.write(self.style.MIGRATE_HEADING(f"{key} — {business_defaults.DEFAULTS[key]}: {len(rows)}"))
            for violation in rows:
                self.stdout.write(f"  {violation.get('source_id', '')}: {business_defaults.describe(violation)}")
        self.stdout.write(f"run uid: {report.run_uid}")
