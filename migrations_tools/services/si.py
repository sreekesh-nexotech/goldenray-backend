"""The Site Inspection V2 (SQLite) import plan — PLAN §7.5 "Site Inspection". Run ``import_pa`` first.

The source is the app's SQLite file (``flarize-site-inspection.db``, opened read-only). Engineers become Field Engineer
users (placeholder addresses, reset links after they are fixed), customers are matched by phone, inspections and their
children are imported column by column by ``site_inspections.services.legacy_import`` (type coercion, recomputed
system type and equipment status, photos to private media, rectangles in ``LEGACY_CONTAINER`` space, approvals without
OTP). A legacy PA inspection carries ``agreement_uid = uuid5(SI_AGREEMENT_NAMESPACE, "PA:<agreement id>")``, the uid
``import_pa`` gave the agreement; the plan lists every inspection whose agreement is not on the platform.
"""

from __future__ import annotations

from core.models import LegacyMap
from migrations_tools.services.runner import Context, Plan, Step
from site_inspections.services import legacy_import as si_import

SI = LegacyMap.SourceSystem.SI
INSPECTION_TABLES = (
    "site_inspections",
    "site_inspection_photos",
    "site_inspection_annotations",
    "site_inspection_equipment_assessments",
    "site_inspection_approvals",
    "site_inspection_observations",
)


def _engineers(rows, ctx: Context) -> dict:
    return {"engineers": si_import.import_engineers(rows["engineers"], logins=rows["engineer_users"], user=ctx.user)}


def _customers(rows, ctx: Context) -> dict:
    return {"customers": si_import.import_customers(rows["customers"], user=ctx.user)}


def agreement_links(rows: list[dict]) -> dict:
    """Legacy PA inspections whose agreement ``import_pa`` did not bring over (``agreement_not_imported``, listed)."""
    from agreements.models import Agreement

    violations, linked = [], 0
    for row in rows:
        uid = si_import.agreement_uid(row.get("purchase_agreement_id"))
        if uid is None:
            continue
        if Agreement.all_objects.filter(uid=uid).exists():
            linked += 1
            continue
        violations.append(
            {
                "source_table": "site_inspections",
                "source_id": str(row.get("id")),
                "code": "agreement_not_imported",
                "severity": "warning",
                "message": (
                    f"{row.get('site_visit_no')}: purchase agreement {row.get('purchase_agreement_id')!r} is not on the platform "
                    "(run import_pa first, or the browser profile holding it was cleared)."
                ),
            }
        )
    return {"created": 0, "updated": 0, "skipped": linked, "violations": violations}


def _inspections(rows, ctx: Context) -> dict:
    """Inspections and their children in dependency order (one batch: children resolve their inspection through the map)."""
    result = {"site_inspections": si_import.import_inspections(rows["site_inspections"], user=ctx.user)}
    result["site_inspection_photos"] = si_import.import_photos(rows["site_inspection_photos"], user=ctx.user)
    si_import.link_layout(rows["site_inspections"])
    result["site_inspection_annotations"] = si_import.import_annotations(rows["site_inspection_annotations"], user=ctx.user)
    result["site_inspection_equipment_assessments"] = si_import.import_equipment(rows["site_inspection_equipment_assessments"], user=ctx.user)
    result["site_inspection_approvals"] = si_import.import_approvals(rows["site_inspection_approvals"], user=ctx.user)
    result["site_inspection_observations"] = si_import.import_observations(rows["site_inspection_observations"], user=ctx.user)
    result["agreement links"] = agreement_links(rows["site_inspections"])
    return result


PLAN = Plan(
    source_system=SI,
    steps=(
        Step("si.engineers", "users", ("engineers", "engineer_users"), _engineers, "engineers → Field Engineer users (no password, placeholder e-mail)"),
        Step("si.customers", "masters", ("customers",), _customers, "customers matched by phone (unmatched created, SI_IMPORT)"),
        Step("si.inspections", "transactional", INSPECTION_TABLES, _inspections, "inspections, photos, annotations, equipment, approvals, observations"),
    ),
    not_migrated={
        "purchase_agreements": "agreements come from the Purchase Agreement page (import_pa); the SI copy is not the source",
        "engineer_sessions": "sessions are not migrated",
        "site_inspection_activity": "generic UPDATED lines only; the platform's activity is the audit log",
        "site_inspection_materials": "dead table (no V2 screen writes it)",
        "site_inspection_documents": "dead table (no V2 screen writes it)",
    },
    # a login is accounted for through its engineer (one user per engineer)
    row_keys={"engineer_users": lambda rows: [("engineers", str(row["engineer_id"])) for row in rows if row.get("engineer_id")]},
)
