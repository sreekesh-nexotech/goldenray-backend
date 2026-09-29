"""The main backend (``GoldenApp``) import plan — PLAN §7.3, in FK order. Run ``import_cms`` first: job applications
find their posting through the CMS map of ``careers_job_position``."""

from __future__ import annotations

from accounts.services import legacy_import as accounts_import
from bom.services import legacy_import as bom_import
from calculators.services import legacy_import as calculators_import
from careers.services import legacy_import as careers_import
from catalog.services import legacy_import as catalog_import
from company.services import legacy_import as company_import
from core.models import LegacyMap
from emi.services import legacy_import as emi_import
from leads.services import legacy_import as leads_import
from migrations_tools.services.runner import Context, Plan, Step
from pricing.services import legacy_import as pricing_import
from quotations.services import legacy_import as quotations_import
from reference.services import legacy_import as reference_import
from seo.services import legacy_import as seo_import

BACKEND = LegacyMap.SourceSystem.BACKEND
REFERENCE_IMPORTERS = {
    "kseb_tariffs": reference_import.import_tariffs,
    "device_types": reference_import.import_device_types,
    "wattages": reference_import.import_wattages,
    "room_size": reference_import.import_room_sizes,
    "ev_cars": reference_import.import_ev_cars,
    "ev_scooters": reference_import.import_ev_scooters,
    "pincodes": reference_import.import_pincodes,
}
CATALOG_TABLES = ("solar_panels", "solar_inverters", "batteries", "bom_category", "bom_catalogitem", "bom_itemtier", "bom_globalcosts", "bom_marketrate", "bom_offer")
BOM_TABLES = ("bom_bomtemplate", "bom_bomslot", "bom_bomfixeditem", "bom_structuretemplate", "bom_structuretemplateitem", "bom_tubeweight")
EMI_TABLES = ("emi_bank", "emi_interest_rate_rule", "emi_subsidy_rule", "emi_calculator_settings", "emi_system_size")
LEADS_TABLES = ("affiliate_application", "warranty_service_request", "customer_installations", "lead_collection_home")
CAREERS_TABLES = ("job_application", "job_application_note", "job_application_event")
QUOTATIONS_TABLES = ("bom_quotationtestimonial", "sent_quotes")


def _users(rows, ctx: Context) -> dict:
    return {"auth_user": accounts_import.import_backend_users(rows["auth_user"], user=ctx.user)}


def _reference(rows, ctx: Context) -> dict:
    return {table: importer(rows[table], user=ctx.user) for table, importer in REFERENCE_IMPORTERS.items()}


def _catalog_pricing(rows, ctx: Context) -> dict:
    """Catalog first; the prices it returns are written by pricing in the same batch (they travel together on resume)."""
    products = catalog_import.import_goldenray_products(rows["solar_panels"], rows["solar_inverters"], rows["batteries"], user=ctx.user)
    bom_catalog = catalog_import.import_bom_catalog(rows["bom_category"], rows["bom_catalogitem"], rows["bom_itemtier"], user=ctx.user)
    return {
        "catalog.website_products": products,
        "catalog.bom_catalog": bom_catalog,
        "pricing.prices": pricing_import.import_prices([*products.get("prices", []), *bom_catalog.get("prices", [])], user=ctx.user),
        "bom_globalcosts": pricing_import.import_bom_global_costs(rows["bom_globalcosts"], user=ctx.user),
        "bom_marketrate": pricing_import.import_bom_market_rates(rows["bom_marketrate"], user=ctx.user),
        "bom_offer": pricing_import.import_bom_offers(rows["bom_offer"], user=ctx.user),
    }


def _bom(rows, ctx: Context) -> dict:
    return {"bom.goldenray": bom_import.import_goldenray_bom(*(rows[table] for table in BOM_TABLES), user=ctx.user)}


def _calculators(rows, ctx: Context) -> dict:
    return calculators_import.import_all(solar_installations=rows["solar_installations"], solar_installation_new=rows["solar_installation_new"], user=ctx.user)


def _emi(rows, ctx: Context) -> dict:
    return emi_import.import_all(
        banks=rows["emi_bank"],
        interest_rules=rows["emi_interest_rate_rule"],
        subsidy_rules=rows["emi_subsidy_rule"],
        settings=rows["emi_calculator_settings"],
        system_sizes=rows["emi_system_size"],
        user=ctx.user,
    )


def _company(rows, ctx: Context) -> dict:
    return {"bom_quotationsettings": company_import.import_quotation_settings(rows["bom_quotationsettings"], read_file=ctx.read_file, user=ctx.user)}


def _seo(rows, ctx: Context) -> dict:
    return {"goldenray_metadata": seo_import.import_page_metadata(rows["goldenray_metadata"], user=ctx.user)}


def _leads(rows, ctx: Context) -> dict:
    # ``solar_installations``/``solar_installation_new`` are the calculators' sizing tables (DV-82, imported by the
    # calculators step); leads' own report of them (DV-58) would only repeat that, so they are not passed here.
    results = leads_import.import_all({table: rows[table] for table in LEADS_TABLES}, user=ctx.user)
    return {table: results[table] for table in LEADS_TABLES}


def _careers(rows, ctx: Context) -> dict:
    read_file = ctx.read_file or (lambda path: None)
    return {
        "job_application": careers_import.import_applications(rows["job_application"], read_file=read_file, user=ctx.user),
        "job_application_note": careers_import.import_application_notes(rows["job_application_note"], user=ctx.user),
        "job_application_event": careers_import.import_application_events(rows["job_application_event"], user=ctx.user),
    }


def _quotations(rows, ctx: Context) -> dict:
    """Homeowner testimonials (``show_on_website``) and the website's sent-quote links → the quotations package's
    tables (DV-120); an uploaded testimonial photo is listed (``photo_file_not_migrated``), not copied."""
    return {
        "bom_quotationtestimonial": quotations_import.import_backend_testimonials(rows["bom_quotationtestimonial"], user=ctx.user),
        "sent_quotes": quotations_import.import_sent_quotes(rows["sent_quotes"], user=ctx.user),
    }


PLAN = Plan(
    source_system=BACKEND,
    steps=(
        Step("backend.users", "users", ("auth_user",), _users, "/bom/ superusers → Admin (forced reset)"),
        Step("backend.reference", "masters", tuple(REFERENCE_IMPORTERS), _reference, "tariffs, device types, wattages, room sizes, EVs, pincodes"),
        Step("backend.catalog_pricing", "masters", CATALOG_TABLES, _catalog_pricing, "website products, BOM catalog, LIST prices, global costs, market rates, offers"),
        Step("backend.bom", "masters", BOM_TABLES, _bom, "BOM templates, slots (qty_rule), fixed items, structures, tube weights"),
        Step("backend.calculators", "masters", ("solar_installations", "solar_installation_new"), _calculators, "calculator sizing tables (DV-82)"),
        Step("backend.emi", "masters", EMI_TABLES, _emi, "EMI banks, rules, settings, sizes (DV-83)"),
        Step("backend.company", "masters", ("bom_quotationsettings",), _company, "quotation offer settings → company profile"),
        Step("backend.seo", "content", ("goldenray_metadata",), _seo, "page metadata"),
        Step("backend.leads", "transactional", LEADS_TABLES, _leads, "affiliate applications, warranty requests, installations, leads"),
        Step("backend.careers", "transactional", CAREERS_TABLES, _careers, "job applications (+ private files), notes, events"),
        Step("backend.quotations", "transactional", QUOTATIONS_TABLES, _quotations, "homeowner testimonials, sent-quote links → e-mail log (DV-120)"),
    ),
    not_migrated={
        "send_quote_junk": "PLAN §7.3: dropped",
        "django_admin_log": "Django admin history is not imported",
        "django_session": "sessions are not migrated",
        "django_migrations": "framework table",
        "django_content_type": "framework table",
        "auth_permission": "framework table (permissions come from the registry)",
        "auth_group": "unused",
        "auth_group_permissions": "unused",
        "auth_user_groups": "unused",
        "auth_user_user_permissions": "unused",
    },
)
