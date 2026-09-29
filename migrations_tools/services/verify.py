"""``verify_migration`` for the website sources (PLAN §7.6 checks 1–7, 10 and 12; 8, 9 and 11 belong to Flarize,
quotations and eSSL).

Every check returns a :class:`CheckResult` (``pass`` / ``fail`` / ``skipped`` with the reason). A check that needs
something that was not given (a source URL, the legacy HTTP API) is ``skipped`` and says what it needs; the command
fails when any check fails, and — unless ``allow_skipped`` — when a required check was skipped.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from django.apps import apps
from django.db import connection, models

from core.models import LegacyMap
from migrations_tools.services import backend as backend_plan
from migrations_tools.services import cms as cms_plan
from migrations_tools.services import parity
from migrations_tools.services.http import head_status
from migrations_tools.services.runner import BATCH_ACTION, RUN_OBJECT, Plan
from migrations_tools.services.source import Source, checksum

PLANS: dict[str, Plan] = {"CMS": cms_plan.PLAN, "BACKEND": backend_plan.PLAN}
MAX_DETAILS = 50


@dataclass
class CheckResult:
    number: int
    name: str
    status: str = "pass"
    summary: str = ""
    details: list[str] = field(default_factory=list)

    def fail(self, detail: str) -> None:
        self.status = "fail"
        if len(self.details) < MAX_DETAILS:
            self.details.append(detail)

    def as_dict(self) -> dict:
        return {"number": self.number, "name": self.name, "status": self.status, "summary": self.summary, "details": self.details}


def skipped(number: int, name: str, reason: str) -> CheckResult:
    return CheckResult(number, name, "skipped", reason)


def base_id(source_id: str) -> str:
    """``"1:install_rate"`` → ``"1"``: importers that explode one row into several targets suffix the source id."""
    return str(source_id).split(":", 1)[0]


def latest_batches(system: str) -> dict[str, dict]:
    """The most recent committed batch audit row of every step of ``system`` (``after`` + ``at``)."""
    from audit.models import AuditLog

    rows = AuditLog.objects.filter(action=BATCH_ACTION, object_type=RUN_OBJECT, after__source_system=system, after__dry_run=False).order_by("at", "id")
    latest = {}
    for row in rows:
        latest[row.after["step"]] = {**row.after, "at": row.at, "run_uid": str(row.object_uid)}
    return latest


class Verifier:
    def __init__(
        self,
        *,
        sources: dict[str, Source | None],
        new,
        legacy_cms=None,
        offline: bool = False,
        corpus_dirs: dict[str, Path] | None = None,
        list_prices_as_release: bool = False,
    ):
        self.sources = sources  # system → Source (None: not given)
        self.list_prices_as_release = list_prices_as_release
        self.new = new
        self.legacy_cms = legacy_cms
        self.offline = offline
        self.corpus_dirs = corpus_dirs or parity.default_corpus_dirs()

    # 1 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    def row_counts(self) -> CheckResult:
        result = CheckResult(1, "row counts vs core_legacy_map")
        compared = 0
        for system, source in self.sources.items():
            if source is None:
                return skipped(1, result.name, f"needs the {system} source (--{system.lower()}-url)")
            plan = PLANS[system]
            batches = latest_batches(system)
            listed: dict[str, set[str]] = {}
            for batch in batches.values():
                for table, ids in (batch.get("listed") or {}).items():
                    listed.setdefault(table, set()).update(ids)
            for table in plan.tables:
                rows = source.rows(table)
                ids = {str(row["id"]) for row in rows if "id" in row}
                mapped = {base_id(value) for value in LegacyMap.objects.filter(source_system=system, source_table=table).values_list("source_id", flat=True)}
                missing = sorted(ids - mapped - listed.get(table, set()), key=lambda value: (len(value), value))
                compared += len(rows)
                if missing:
                    result.fail(f"{system} {table}: {len(missing)} of {len(rows)} rows neither mapped nor listed ({', '.join(missing[:10])})")
                orphans = mapped - ids
                if orphans:
                    result.fail(f"{system} {table}: {len(orphans)} mapped source ids no longer exist in the source ({', '.join(sorted(orphans)[:10])})")
            known = set(plan.tables) | set(plan.not_migrated)
            for table in source.table_names():
                if table not in known and source.count(table):
                    result.fail(f"{system} {table}: {source.count(table)} rows in a table no step imports and no reason lists")
        result.summary = f"{compared} source rows compared"
        return result

    # 2 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    def integrity(self) -> CheckResult:
        result = CheckResult(2, "referential integrity")
        systems = list(self.sources)
        targets = LegacyMap.objects.filter(source_system__in=systems).values_list("target_table", flat=True).distinct()
        checked = 0
        with connection.cursor() as cursor:
            for table in targets:
                cursor.execute(
                    "SELECT count(*) FROM core_legacy_map m WHERE m.source_system = ANY(%s) AND m.target_table = %s "
                    "AND NOT EXISTS (SELECT 1 FROM " + connection.ops.quote_name(table) + " t WHERE t.id = m.target_id)",
                    [systems, table],
                )
                dangling = cursor.fetchone()[0]
                checked += 1
                if dangling:
                    result.fail(f"core_legacy_map → {table}: {dangling} dangling targets")
            cursor.execute("SELECT conrelid::regclass::text, conname FROM pg_constraint WHERE contype = 'f' AND NOT convalidated")
            for table, name in cursor.fetchall():
                result.fail(f"{table}: foreign key {name} is NOT VALID")
        for model, field_ in self._unconstrained_foreign_keys():
            column = field_.column
            related = field_.related_model
            dangling = model._base_manager.exclude(**{f"{column}__isnull": True}).exclude(**{f"{column}__in": related._base_manager.values("pk")}).count()
            if dangling:
                result.fail(f"{model._meta.db_table}.{column}: {dangling} values point at no {related._meta.db_table} row")
        result.summary = f"{checked} mapped target tables, every database foreign key validated, unconstrained foreign keys resolved"
        return result

    @staticmethod
    def _unconstrained_foreign_keys():
        for model in apps.get_models():
            if model._meta.managed is False and model._meta.db_table != "audit_log":
                continue
            for field_ in model._meta.concrete_fields:
                if isinstance(field_, models.ForeignKey) and not field_.db_constraint:
                    yield model, field_

    # 3 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    def delivery(self) -> CheckResult:
        result = CheckResult(3, "delivery parity (collections, page-content, faqs, job-positions)")
        source = self.sources.get("CMS")
        if "CMS" not in self.sources:
            return skipped(3, result.name, "CMS not verified")
        if source is None or self.legacy_cms is None:
            return skipped(3, result.name, "needs --cms-url and --legacy-cms-api (the legacy CMS HTTP API)")
        cases = []
        for build in (parity.collection_cases, parity.page_cases, parity.faq_cases, parity.position_cases):
            cases.extend(build(source, self.legacy_cms, self.new))
        for case in cases:
            difference = case.difference
            if difference:
                result.fail(f"{case.name}: {difference}")
        statuses = {}
        for case in cases:
            statuses[case.legacy_status] = statuses.get(case.legacy_status, 0) + 1
        result.summary = f"{len(cases)} requests compared ({', '.join(f'{count}× {status}' for status, count in sorted(statuses.items()))}); {len(result.details)} differ"
        return result

    # 4 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    def media(self) -> CheckResult:
        from media.models import MediaAsset
        from media.services.storage import StorageError, storage_for

        result = CheckResult(4, "media: CDN HEAD 200 and stored checksums")
        targets = LegacyMap.objects.filter(source_system__in=list(self.sources), target_table=MediaAsset._meta.db_table).values("target_id")
        assets = list(MediaAsset.all_objects.filter(pk__in=targets, deleted_at__isnull=True))
        from careers.models import JobApplication

        applications = LegacyMap.objects.filter(source_system="BACKEND", source_table="job_application").values("target_id")
        for application in JobApplication.all_objects.filter(pk__in=applications).select_related("resume", "portfolio"):
            assets.extend(asset for asset in (application.resume, application.portfolio) if asset is not None)
        heads = files = 0
        for asset in assets:
            if asset.is_public and asset.cdn_url and not self.offline:
                heads += 1
                status = head_status(asset.cdn_url)
                if status != 200:
                    result.fail(f"{asset.uid} {asset.cdn_url}: HEAD {status}")
            if asset.checksum_sha256:
                try:
                    data = storage_for(asset.visibility).read(asset.file)
                except (StorageError, OSError):
                    if not asset.is_public:
                        result.fail(f"{asset.uid}: private file {asset.file} is missing")
                    continue
                files += 1
                if hashlib.sha256(data).hexdigest() != asset.checksum_sha256:
                    result.fail(f"{asset.uid}: {asset.file} checksum differs")
        result.summary = f"{len(assets)} migrated assets, {heads} CDN URLs HEAD-checked{' (offline: HEAD skipped)' if self.offline else ''}, {files} stored files checksummed"
        return result

    # 5 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    def slugs(self) -> CheckResult:
        name = "historical and current slugs resolve"
        if "CMS" not in self.sources:
            return skipped(5, name, "CMS not verified")
        if self.sources["CMS"] is None:
            return skipped(5, name, "needs --cms-url")
        count, problems = parity.slug_problems(self.sources["CMS"], self.new)
        result = CheckResult(5, name, summary=f"{count} slugs requested")
        for problem in problems:
            result.fail(problem)
        return result

    # 6 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    def users(self) -> CheckResult:
        from accounts.models import PasswordReset, User
        from accounts.services.legacy_import import BACKEND_USER_TABLE, CMS_USER_TABLE, MIGRATED_DOMAIN

        result = CheckResult(6, "users: role, reset link, old passwords refused")
        tables = [table for system, table in (("CMS", CMS_USER_TABLE), ("BACKEND", BACKEND_USER_TABLE)) if system in self.sources]
        targets = LegacyMap.objects.filter(source_system__in=list(self.sources), source_table__in=tables).values("target_id")
        users = list(User.all_objects.filter(pk__in=targets).select_related("role"))
        for account in users:
            if account.deleted_at is not None:
                continue
            if account.role is None or account.role.deleted_at is not None:
                result.fail(f"{account.email}: no live role")
            if account.has_usable_password() and account.password_changed_at is None:
                result.fail(f"{account.email}: carries a usable password it never set on the platform")
            real_address = not account.email.endswith(f"@{MIGRATED_DOMAIN}")
            if account.is_active and account.must_reset_password and real_address and not PasswordReset.all_objects.filter(user=account).exists():
                result.fail(f"{account.email}: no reset link issued (run the import with --send-reset-links at the cutover)")
        result.summary = f"{len(users)} migrated accounts"
        return result

    # 7 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
    def pricing(self) -> CheckResult:
        name = "pricing: current LIST price == BOM / website price"
        if "BACKEND" not in self.sources:
            return skipped(7, name, "BACKEND not verified")
        source = self.sources["BACKEND"]
        if source is None:
            return skipped(7, name, "needs --backend-url")
        from catalog.models import Component
        from pricing.services.prices import current_row

        result = CheckResult(7, name)
        compared = flarize_owned = 0
        for table, amount_column, per_watt_column in (("bom_catalogitem", "price", "per_watt"), ("batteries", "battery_price", None)):
            for row in source.rows(table):
                if row.get(amount_column) in (None, ""):
                    continue
                target = LegacyMap.objects.filter(source_system="BACKEND", source_table=table, source_id=str(row["id"])).values_list("target_id", flat=True).first()
                component = Component.all_objects.filter(pk=target).first() if target else None
                if component is None:
                    result.fail(f"{table} {row['id']}: no component")
                    continue
                if LegacyMap.objects.filter(source_system="FLARIZE", target_table=Component._meta.db_table, target_id=component.pk).exists():
                    flarize_owned += 1  # D-2: Flarize catalog.json is authoritative for this component
                    continue
                compared += 1
                price = current_row(component, "LIST")
                expected = Decimal(str(row[amount_column]))
                if price is None:
                    result.fail(f"{component.sku}: no current LIST price (source {expected})")
                    continue
                if price.amount != expected:
                    result.fail(f"{component.sku}: LIST {price.amount} ≠ source {expected}")
                if per_watt_column and row.get(per_watt_column) not in (None, "") and price.per_watt != Decimal(str(row[per_watt_column])):
                    result.fail(f"{component.sku}: per watt {price.per_watt} ≠ source {row[per_watt_column]}")
        result.summary = f"{compared} prices compared, {flarize_owned} components owned by the Flarize catalog (D-2) not compared"
        return result

    # 10 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
    def calculators(self) -> CheckResult:
        name = "calculators: committed corpora answer identically"
        if "BACKEND" not in self.sources:
            return skipped(10, name, "BACKEND not verified")
        missing = [str(path) for path in self.corpus_dirs.values() if not path.is_dir()]
        if missing:
            return skipped(10, name, f"corpus not found: {', '.join(missing)} (--corpus-dir)")
        from pricing.services.releases import current_release

        if self.list_prices_as_release:
            with parity.list_prices_as_release():
                replayed, differences = parity.calculator_differences(self.new, self.corpus_dirs)
        else:
            replayed, differences = parity.calculator_differences(self.new, self.corpus_dirs)
        result = CheckResult(10, name, summary=f"{replayed} recorded requests replayed; {len(differences)} differ")
        if self.list_prices_as_release:
            result.summary += " (website product prices: the imported current LIST prices standing in for a PriceRelease — rehearsal mode)"
        elif differences and current_release() is None:
            result.summary += " — no PriceRelease is published: the advanced calculator prices hybrid batteries only from a release (DV-85)"
        for difference in differences:
            result.fail(difference)
        return result

    # 12 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
    def audit(self) -> CheckResult:
        result = CheckResult(12, "audit: one batch row per import step with counts and source checksum")
        steps = 0
        for system in self.sources:
            plan = PLANS[system]
            batches = latest_batches(system)
            source = self.sources[system]
            for step in plan.steps:
                steps += 1
                batch = batches.get(step.name)
                if batch is None:
                    result.fail(f"{step.name}: no batch audit row (step never imported)")
                    continue
                if not batch.get("counts") or not batch.get("checksum") or set(batch.get("tables", {})) != set(step.tables):
                    result.fail(f"{step.name}: batch row lacks counts, tables or checksum")
                    continue
                if source is not None:
                    for table in step.tables:
                        current = checksum(source.rows(table))
                        if batch["tables"][table]["checksum"] != current:
                            result.fail(f"{step.name}: {table} changed in the source since the import of {batch['at']:%Y-%m-%d %H:%M} (re-run the import)")
        result.summary = f"{steps} steps checked"
        return result

    def run(self, only: set[int] | None = None) -> list[CheckResult]:
        checks = {1: self.row_counts, 2: self.integrity, 3: self.delivery, 4: self.media, 5: self.slugs, 6: self.users, 7: self.pricing, 10: self.calculators, 12: self.audit}
        results = []
        for number, check in checks.items():
            if only and number not in only:
                continue
            try:
                results.append(check())
            except Exception as exc:  # noqa: BLE001 - a crashing check is a failed check, the others still run
                results.append(CheckResult(number, check.__name__, "fail", f"check crashed: {exc.__class__.__name__}: {exc}"))
        return results
