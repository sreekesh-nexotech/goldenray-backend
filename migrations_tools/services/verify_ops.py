"""``verify_migration`` checks of the operational sources (PLAN §7.6 #8, #9, #11).

* **#8 Packs** — PackRelease #1 against the Flarize reference outputs: the pack results of the real Flarize JavaScript
  over the same data (``packs/tests/golden/generate_packs.mjs`` → ``flarize_packs.json``: ``buildBom`` + ``pricePack``
  + ``runPackageChecker`` for every pack of the approved configuration). Every pack Flarize could price (a market rate
  set, ``pricing.status == COMPLETE``) and whose checker did not BLOCK it must be in the release with the same
  ``customer_price_incl_gst``; a priced pack the checker blocks must be excluded and reported with
  ``PACK_ENGINEERING_BLOCKED`` (business default B-4: no automatic waiver); the publish report must hold no BLOCK item
  (a release with one cannot be published — this proves nobody bypassed it). When the Flarize folder is given, the
  reference must have been generated from the same ``pack-config.json`` / ``catalog.json`` / ``packages.proposed.json``
  / ``battery-master.json`` (SHA-256 of the files) — otherwise it is stale and the check fails.
* **#9 Quotations** — every imported Flarize document is frozen byte for byte and re-renders with equal hashes: for each
  issued legacy version, the SHA-256 of the source document (``quotation-state.json``, when given) == the stored
  ``document_payload_sha256`` == the SHA-256 of the canonical payload a render job gets
  (``documents.services.jobs.canonical_payload``, the ``payload_sha256`` of a re-render); and the payload renders through
  the quotation template in both languages (HTML, in memory — nothing is written). Two renders of the same payload give
  the same HTML (the document is deterministic). PDF page count / text diff against archived PDFs (PLAN §7.6 #9) needs
  the archive, which the reference estate does not hold; the HTML hash is reported for comparison with the archive run.
* **#11 Attendance** — raw punch accounting after dedup (every eSSL ``attendance_raw`` row is mapped to a platform
  punch or listed; mapped rows − distinct punches = the duplicates collapsed; every target exists) and the v3/v4
  per-employee-month diff, rebuilt read-only (``attendance.services.legacy_import.diff_report``), written for HR. The
  check passes only once HR signed that exact report off (``--attendance-signoff <sha256 of the report>``); until then
  it is ``skipped`` (a failure unless ``--allow-skipped``).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder

from core.models import LegacyMap
from migrations_tools.services.source import JsonFilesSource, Source, document

REFERENCE_FILES = ("catalog.json", "pack-config.json", "packages.proposed.json", "battery-master.json")
DEFAULT_REFERENCE = Path(settings.BASE_DIR) / "packs" / "tests" / "golden" / "flarize_packs.json"


def default_reference() -> Path:
    return DEFAULT_REFERENCE


# ── #8 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def packs(result, source: Source | None, reference_path: Path) -> None:
    from packs.models import PackRelease, ReleasePack
    from packs.services import engine

    release = PackRelease.objects.filter(number=1).select_related("config_version", "price_release").first()
    if release is None:
        result.fail("PackRelease #1 is not published (import_flarize publishes it in its last step)")
        return
    try:
        reference = json.loads(Path(reference_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result.fail(f"reference {reference_path}: not readable ({exc.__class__.__name__})")
        return
    if isinstance(source, JsonFilesSource):
        for name in REFERENCE_FILES:
            data = source.read_file(name)
            recorded = (reference.get("sources") or {}).get(f"data/{name}")
            if data is not None and recorded and hashlib.sha256(data).hexdigest() != recorded:
                result.fail(f"reference {reference_path} was generated from another {name} (regenerate it with packs/tests/golden/generate_packs.mjs)")
    if reference.get("configVersion") not in (None, release.config_version.number):
        result.fail(f"reference is for pack configuration v{reference.get('configVersion')}, PackRelease #1 is v{release.config_version.number}")
        return
    specs = engine.enumerate_packs(release.config_version.config or {})
    cases = reference.get("cases") or []
    by_input = {(spec.system, spec.size, spec.tier, spec.future, spec.battery): spec for spec in specs}
    if len(specs) != len(cases):
        result.fail(f"reference has {len(cases)} packs, the approved configuration enumerates {len(specs)}")
        return
    rows = {row.key: row for row in ReleasePack.objects.filter(release=release)}
    report = release.publish_report or {}
    matrix = {row.get("key"): row for row in report.get("matrix") or []}
    priced = compared = blocked = 0
    for case in cases:
        entry = case.get("input") or {}
        spec = by_input.get((entry.get("systemType"), entry.get("size"), entry.get("tier"), entry.get("futureSystemSize") or "", entry.get("batteryQuantity")))
        if spec is None:
            result.fail(f"reference case {entry} is not a pack of the approved configuration (regenerate the reference)")
            continue
        pricing = case.get("pricing") or {}
        if "error" in case or pricing.get("status") != "COMPLETE":
            if spec.key in rows:
                result.fail(f"{spec.key}: released although Flarize could not price it ({pricing.get('status') or case.get('error')})")
            continue
        priced += 1
        if (case.get("checker") or {}).get("status") == "BLOCKED":
            blocked += 1
            if spec.key in rows:
                result.fail(f"{spec.key}: released although the Flarize checker blocks it (B-4: no automatic waiver)")
            elif "PACK_ENGINEERING_BLOCKED" not in (matrix.get(spec.key) or {}).get("reasons", []):
                result.fail(f"{spec.key}: excluded without the PACK_ENGINEERING_BLOCKED reason in the publish report")
            continue
        row = rows.get(spec.key)
        expected = Decimal(str(pricing["customer"]["sellingPriceIncludingGST"]))
        if row is None:
            result.fail(f"{spec.key}: missing from PackRelease #1 (Flarize price {expected})")
            continue
        compared += 1
        if row.customer_price_incl_gst != expected:
            result.fail(f"{spec.key}: customer_price_incl_gst {row.customer_price_incl_gst} ≠ Flarize {expected}")
    extra = set(rows) - {spec.key for spec in specs}
    for key in sorted(extra):
        result.fail(f"{key}: in PackRelease #1 but not a pack of the approved configuration")
    counts = report.get("counts") or {}
    if counts.get("BLOCK"):
        result.fail(f"the publish report holds {counts['BLOCK']} BLOCK item(s)")
    result.summary = (
        f"PackRelease #1 (config v{release.config_version.number}, PriceRelease #{release.price_release.number}): {compared} packs equal to the Flarize reference, "
        f"{priced} priced by Flarize ({blocked} blocked by the checker, excluded — B-4), {len(specs) - priced} without a market rate; "
        f"publish report BLOCK {counts.get('BLOCK', 0)}, WARN {counts.get('WARN', 0)}"
    )


# ── #9 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _render_html(version, language: str) -> str:
    from documents.models import RenderJob
    from documents.services import templates
    from documents.services.jobs import canonical_payload

    payload, _ = canonical_payload(version.document_payload)
    job = RenderJob(kind="QUOTATION", object_type="quotations.version", object_uid=version.uid, template="default", language=language, payload=payload)
    job.created_at = version.issued_at
    return templates.render_html(job)


def quotations(result, source: Source | None) -> None:
    from documents.services.jobs import canonical_payload
    from engines.frozen import sha256_hex
    from quotations.models import Version, VersionStatus

    state = document(source, "quotation-state.json") if source is not None else None
    documents = (state or {}).get("documents") or {}
    mapped = LegacyMap.objects.filter(source_system="FLARIZE", source_table="quotation-state.json").values("target_id")
    versions = list(Version.objects.filter(quotation_id__in=mapped, legacy=True).exclude(status=VersionStatus.DRAFT).select_related("quotation").order_by("quotation__legacy_ref", "number"))
    html_hashes = []
    for version in versions:
        ref = f"{version.quotation.legacy_ref} v{version.number}"
        stored = version.document_payload_sha256
        if not version.document_payload or not stored:
            result.fail(f"{ref}: issued without a frozen document")
            continue
        if sha256_hex(version.document_payload) != stored:
            result.fail(f"{ref}: the stored document no longer hashes to its document_payload_sha256")
        _, render_hash = canonical_payload(version.document_payload)
        if render_hash != stored:
            result.fail(f"{ref}: a re-render would carry payload_sha256 {render_hash[:12]}…, not the frozen {stored[:12]}…")
        if state is not None:
            source_doc = next((doc for doc in documents.get(version.quotation.legacy_ref) or [] if (doc.get("version") or 1) == version.number), None)
            if source_doc is None:
                result.fail(f"{ref}: no document version {version.number} in quotation-state.json")
            elif sha256_hex(source_doc) != stored:
                result.fail(f"{ref}: differs from the source document (frozen copies are never overwritten: frozen_document_changed)")
        for language in ("en", "ml"):
            try:
                first, second = _render_html(version, language), _render_html(version, language)
            except Exception as exc:  # noqa: BLE001 - a template failure is this check's finding
                result.fail(f"{ref} ({language}): does not render — {exc.__class__.__name__}: {exc}")
                continue
            if first != second:
                result.fail(f"{ref} ({language}): two renders of the same payload differ")
            html_hashes.append(hashlib.sha256(first.encode()).hexdigest())
    if state is not None:
        issued = sum(1 for qid in documents if documents[qid])
        imported = len({version.quotation.legacy_ref for version in versions})
        if imported < issued:
            result.fail(f"{issued - imported} quotation(s) with documents in quotation-state.json have no issued legacy version")
    result.summary = (
        f"{len(versions)} frozen documents: stored, source{' ' if state is not None else ' (not given) '}and re-render hashes equal; "
        f"{len(html_hashes)} renders (en + ml) deterministic; digest of the renders {hashlib.sha256(''.join(html_hashes).encode()).hexdigest()[:16]} "
        "(archived PDFs not available for the page-count/text diff)"
    )


# ── #11 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def report_sha256(report: dict) -> str:
    return hashlib.sha256(json.dumps(report, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def attendance(result, source: Source, *, report_path: str | None, signoff: str | None):
    """Returns ``"skipped"`` when everything holds but HR has not signed this report off."""
    from attendance.models import RawPunch
    from attendance.services import legacy_import as attendance_import

    raw_rows = source.rows("attendance_raw")
    ids = {str(row["id"]) for row in raw_rows if "id" in row}
    maps = dict(LegacyMap.objects.filter(source_system="ESSL", source_table="attendance_raw").values_list("source_id", "target_id"))
    from migrations_tools.services.verify import latest_batches

    listed = set()
    for batch in latest_batches("ESSL").values():
        listed.update((batch.get("listed") or {}).get("attendance_raw", []))
    missing = sorted(ids - set(maps) - listed, key=lambda value: (len(value), value))
    if missing:
        result.fail(f"attendance_raw: {len(missing)} of {len(ids)} rows neither mapped nor listed ({', '.join(missing[:10])})")
    targets = set(maps.values())
    existing = set(RawPunch.objects.filter(id__in=targets).values_list("id", flat=True)) if targets else set()
    if targets - existing:
        result.fail(f"attendance_raw: {len(targets - existing)} mapped punches no longer exist")
    collapsed = len(maps) - len(targets)
    diff = attendance_import.diff_report(source.rows("attendance"))
    summary = {
        "raw_punches": {"source_rows": len(ids), "mapped": len(maps), "punches": len(targets), "collapsed": collapsed, "listed": len(ids & listed)},
        "days_compared": diff["days_compared"],
        "days_differing": diff["days_differing"],
        "month_totals": attendance_import.month_totals(diff),
        "months": diff["months"],
    }
    digest = report_sha256(summary)
    if report_path:
        Path(report_path).write_text(json.dumps({"sha256": digest, **summary}, cls=DjangoJSONEncoder, indent=1, ensure_ascii=False), encoding="utf-8")
    result.summary = (
        f"{len(ids)} eSSL raw rows → {len(targets)} punches ({collapsed} duplicates collapsed, {len(ids & listed)} listed); "
        f"{diff['days_differing']} of {diff['days_compared']} employee-days differ v3 → v4; diff report sha256 {digest}"
        + (f" written to {report_path}" if report_path else " (--attendance-report FILE to write it)")
    )
    if result.status == "fail":
        return None
    if signoff != digest:
        result.status = "skipped"
        result.summary += " — HR sign-off pending: re-run with --attendance-signoff <that sha256> once HR approved it" if not signoff else " — the signed-off report differs from this one"
        return "skipped"
    result.summary += " — signed off by HR"
    return None
