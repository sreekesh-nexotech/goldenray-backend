"""The Flarize (JSON files) import plan — PLAN §7.4, every row, in FK order, ending with PriceRelease #1 and
PackRelease #1 published through the services.

The source is the Flarize ``data/`` folder (:class:`~migrations_tools.services.source.JsonFilesSource`: one table per
file, holding the parsed document) or a fixture ``{"tables": {"<file>.json": [document]}}``. Each step calls the owning
apps' ``services/legacy_import.py`` functions; the files are never written (PLAN §7.7: the utility keeps running on
them until the cutover is accepted).
"""

from __future__ import annotations

from accounts.services import legacy_import as accounts_import
from bom.services import legacy_import as bom_import
from catalog.services import legacy_import as catalog_import
from company.services import legacy_import as company_import
from core.models import LegacyMap
from customers.services import legacy_import as customers_import
from media.services import legacy_import as media_import
from migrations_tools.services import business_defaults
from migrations_tools.services.runner import Context, Plan, Step
from packs.services import legacy_import as packs_import
from pricing.services import legacy_import as pricing_import
from procurement.services import legacy_import as procurement_import
from projects.services import legacy_import as projects_import
from quotations.services import legacy_import as quotations_import

FLARIZE = LegacyMap.SourceSystem.FLARIZE
ENGINE_CONFIGS = ("energy", "savings", "subsidy", "finance")
CONTENT_FILES = ("quotation-inclusions.json", "tier-display-names.json", "quotation-testimonials.json", "quotation-content.json", "quotation-branding-state.json")


def doc(rows: dict, table: str):
    """The document of a one-file table (``None`` when the file is absent)."""
    return rows[table][0] if rows.get(table) else None


def _users(rows, ctx: Context) -> dict:
    return {"users.json": accounts_import.import_flarize_users(doc(rows, "users.json") or [], user=ctx.user)}


def _cms_assets(rows, ctx: Context) -> dict:
    """``cms-state.json`` is not migrated (the page designer is superseded by quotation content versions); its asset
    files are copied to private media for reference (PLAN §7.4 last row)."""
    state = doc(rows, "cms-state.json") or {}
    assets = list((state.get("assets") or {}).values())
    result = media_import.import_flarize_cms_assets(assets, read_file=ctx.read_file, user=ctx.user)
    listed = {key: len(state.get(key) or {}) for key in ("pages", "pageVersions", "campaigns", "fieldRegistry")}
    if any(listed.values()):
        result["violations"].append(
            {
                "source_table": "cms-state.json",
                "source_id": "page-designer",
                "code": "page_designer_not_migrated",
                "message": f"Flarize page designer state not migrated (superseded by quotation content versions): {', '.join(f'{count} {key}' for key, count in listed.items())}.",
            }
        )
    return {"cms-state.json:assets": result}


def _catalog_pricing(rows, ctx: Context) -> dict:
    """Catalog (authoritative, D-2) → its LIST prices → the catalog.json pricing sections. Every value that differs from
    the main-backend import is reported ``d2_flarize_wins`` (business default B-2)."""
    catalog = doc(rows, "catalog.json") or {}
    battery_master = doc(rows, "battery-master.json")
    result = catalog_import.import_flarize_catalog(catalog, battery_master, user=ctx.user)
    return {
        "catalog.json:catalog": result,
        "pricing.prices": pricing_import.import_prices(result.get("prices") or [], user=ctx.user),
        "catalog.json:pricing": pricing_import.import_flarize_pricing(catalog, user=ctx.user),
    }


def _documents(rows, ctx: Context) -> dict:
    """``cost-config.json``, ``project-rate-card.json``, ``quotation-policy.json`` and the four engine configurations →
    ``pricing_cost_config`` documents + ``pricing_validity_policy``."""
    engine_configs = {name: doc(rows, f"{name}-config.json") for name in ENGINE_CONFIGS if doc(rows, f"{name}-config.json")}
    return {
        "pricing.documents": pricing_import.import_flarize_documents(
            cost_config_json=doc(rows, "cost-config.json"),
            rate_card_json=doc(rows, "project-rate-card.json"),
            quotation_policy_json=doc(rows, "quotation-policy.json"),
            engine_configs=engine_configs or None,
            user=ctx.user,
        )
    }


def _bom(rows, ctx: Context) -> dict:
    """Structures, tube weights, BOM templates, package profiles; then business default B-5 (see backend ``_bom``)."""
    return {
        "catalog.json:bom": bom_import.import_flarize_bom(doc(rows, "catalog.json") or {}, user=ctx.user),
        "B-5 hybrid slot phases": business_defaults.hybrid_phase_mismatches(),
    }


def _procurement(rows, ctx: Context) -> dict:
    """Suppliers, COMMITTED batches, PURCHASE/LANDED prices with their ``version_key`` (history order preserved)."""
    return {
        "procurement": procurement_import.import_flarize_procurement(
            doc(rows, "procurement-state.json") or {}, doc(rows, "procurement-price-master.json"), doc(rows, "commercial-history.json"), user=ctx.user
        )
    }


def _packs(rows, ctx: Context) -> dict:
    """``pack-config.json`` history/approved/draft (+ the registry pins of ``packages.proposed.json``)."""
    return {"pack-config.json": packs_import.import_flarize_pack_config(doc(rows, "pack-config.json") or {}, doc(rows, "packages.proposed.json"), user=ctx.user)}


def _company(rows, ctx: Context) -> dict:
    return {"company-profile.json": company_import.import_flarize_company_profile(doc(rows, "company-profile.json"), user=ctx.user)}


def _content(rows, ctx: Context) -> dict:
    """Masters first; publishing the content version freezes them beside it (quotations decision 8)."""
    return {
        "quotation-inclusions.json": quotations_import.import_flarize_inclusions(doc(rows, "quotation-inclusions.json") or {}, user=ctx.user),
        "tier-display-names.json": quotations_import.import_flarize_tier_names(doc(rows, "tier-display-names.json") or {}, user=ctx.user),
        "quotation-testimonials.json": quotations_import.import_flarize_testimonials(doc(rows, "quotation-testimonials.json") or {}, user=ctx.user),
        "quotation-content.json": quotations_import.import_flarize_content(doc(rows, "quotation-content.json") or {}, user=ctx.user),
        "quotation-branding-state.json": quotations_import.import_flarize_branding(doc(rows, "quotation-branding-state.json") or {}, user=ctx.user),
    }


def _customers(rows, ctx: Context) -> dict:
    return {"customers.json": customers_import.import_flarize_customers(doc(rows, "customers.json") or [], user=ctx.user)}


def _quotations(rows, ctx: Context) -> dict:
    """The counter first (``QUO`` continues after ``lastNumber``), then quotations with their frozen documents byte for
    byte, versions and snapshots."""
    return {
        "quotation-counter.json": quotations_import.import_flarize_counter(doc(rows, "quotation-counter.json") or {}, user=ctx.user),
        "quotation-state.json": quotations_import.import_flarize_quotations(doc(rows, "quotation-state.json") or {}, user=ctx.user),
    }


def _projects(rows, ctx: Context) -> dict:
    """Locked workspaces → projects (+ ``bom_lock``); open workspaces and ``bom-state.json`` are reported, not migrated."""
    state = doc(rows, "workspace-state.json") or {}
    return {
        "workspace-state.json": projects_import.import_flarize_workspace_projects(list((state.get("projects") or {}).values()), user=ctx.user),
        "bom-state.json": projects_import.report_flarize_bom_state(doc(rows, "bom-state.json")),
    }


def _releases(rows, ctx: Context) -> dict:
    """PriceRelease #1 and PackRelease #1 through the services (``publish_initial_releases``). The approved pack
    configuration's market rates win over the imported ``catalog.json`` cells (business default B-3; each replaced cell
    is listed ``pack_config_market_rate_wins``). A BLOCK item stops the step (PLAN §7.4: the business fills the data
    blockers first); the packs a release excludes are listed with their reasons (PLAN §7.6 #8)."""
    from packs.models import PackRelease
    from pricing.models import PriceRelease

    before = PriceRelease.objects.count() + PackRelease.objects.count()
    published = packs_import.publish_initial_releases(user=ctx.user)
    created = PriceRelease.objects.count() + PackRelease.objects.count() - before
    violations = list(published["violations"])
    for row in (published.get("report") or {}).get("matrix") or []:
        if row.get("status") == "EXCLUDED":
            violations.append(
                {
                    "source_table": "pack-config.json:packs",
                    "source_id": row.get("key"),
                    "code": "pack_excluded",
                    "severity": "warning",
                    "message": f"{row.get('key')}: not in PackRelease #{published['pack_release']} ({', '.join(row.get('reasons') or [])}).",
                }
            )
    return {"releases": {"created": created, "updated": 0, "skipped": 0 if created else 1, "violations": violations}}


def _customer_keys(rows):
    return [("customers", str(row["customerId"])) for row in (rows[0] if rows else []) if isinstance(row, dict) and row.get("customerId")]


def _user_keys(rows):
    return [("users.json", str(row["userId"])) for row in (rows[0] if rows else []) if isinstance(row, dict) and row.get("userId")]


def _quotation_keys(rows):
    return [("quotation-state.json", str(qid)) for qid in ((rows[0] if rows else {}).get("quotations") or {})]


def _project_keys(rows):
    return [("workspace_projects", str(project.get("projectId") or key)) for key, project in ((rows[0] if rows else {}).get("projects") or {}).items()]


def _asset_keys(rows):
    return [("cms-state.json:assets", str(asset_id)) for asset_id in ((rows[0] if rows else {}).get("assets") or {})]


PLAN = Plan(
    source_system=FLARIZE,
    steps=(
        Step("flarize.users", "users", ("users.json",), _users, "users → accounts (roles mapped, sales flavour in title, forced reset)"),
        Step("flarize.cms_assets", "media", ("cms-state.json",), _cms_assets, "page-designer asset files → private media, for reference"),
        Step("flarize.catalog_pricing", "masters", ("catalog.json", "battery-master.json"), _catalog_pricing, "catalog (D-2), LIST prices, costs, matrix, market rates, offers"),
        Step(
            "flarize.documents",
            "masters",
            ("cost-config.json", "project-rate-card.json", "quotation-policy.json", *(f"{name}-config.json" for name in ENGINE_CONFIGS)),
            _documents,
            "cost config, rate card, validity policy, energy/savings/subsidy/finance configurations",
        ),
        Step("flarize.bom", "masters", ("catalog.json",), _bom, "structure templates, tube weights, BOM templates, package profiles"),
        Step("flarize.procurement", "masters", ("procurement-state.json", "procurement-price-master.json", "commercial-history.json"), _procurement, "suppliers, batches, PURCHASE/LANDED prices"),
        Step("flarize.packs", "masters", ("pack-config.json", "packages.proposed.json"), _packs, "pack configuration versions (+ registry pins)"),
        Step("flarize.company", "masters", ("company-profile.json",), _company, "company profile (fill only; bank details reported, D-9)"),
        Step("flarize.quotation_content", "content", CONTENT_FILES, _content, "inclusions, tier names, testimonials, content version #1, branding report"),
        Step("flarize.customers", "transactional", ("customers.json",), _customers, "customers (phone → E.164, code kept)"),
        Step("flarize.quotations", "transactional", ("quotation-counter.json", "quotation-state.json"), _quotations, "QUO counter, quotations, frozen documents, snapshots"),
        Step("flarize.projects", "transactional", ("workspace-state.json", "bom-state.json"), _projects, "locked BOM workspaces → projects; open ones reported"),
        Step("flarize.releases", "releases", ("pack-config.json",), _releases, "PriceRelease #1 and PackRelease #1 through the services"),
    ),
    not_migrated={
        "catalog.json.bak": "a backup copy of catalog.json (catalog.json is imported)",
        "cms-state.json": "page designer superseded by quotation content versions (assets copied for reference)",
    },
    row_keys={
        "users.json": _user_keys,
        "customers.json": _customer_keys,
        "quotation-state.json": _quotation_keys,
        "workspace-state.json": _project_keys,
        "cms-state.json": _asset_keys,
    },
)
