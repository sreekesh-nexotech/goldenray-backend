"""The quotation pipeline: one version's tiers priced and frozen from a PackRelease (port of Flarize
``salesOrchestrator.orchestrateThreeTiers`` + ``quotationWorkspace.issueQuotation``, pack mode).

For each tier of the three-option page (the version's tier first, then the other two; an alternative that cannot be
sold is reported in ``unavailable`` and its column prints "Not available" — Flarize ``allowPartialAlternatives``):

1. the released pack of (system type, tier, size, phase, battery configuration, future-ready size) — the platform's
   APPROVED package (``NO_APPROVED_PACKAGE``);
2. Sales swaps validated against the pack's approved alternatives (``engines.bom_builder.get_alternatives``, Sales
   rules: only ``salesSwap`` slots — ``SELECTION_NOT_PERMITTED`` / ``SELECTION_NOT_APPROVED``);
3. the BOM materialised by ``engines.bom_builder.build_bom`` on the release's configuration, pins and PriceRelease
   prices; the project BOM (``engines.bom_domain``) holds its catalog-identified lines, the swaps as overrides and the
   STRUCTURE line of the roof's structure pack (``STRUCTURE_PACK_UNRESOLVED``);
4. the engineering checker with the ACTIVE rule set, then ``attempt_lock`` with every warning acknowledged on behalf
   of the released (approved) pack (``ENGINEERING_BLOCKED``);
5. ``engines.pack_pricing.price_pack`` for the roof, distance and vehicle (``PACK_PRICING_BLOCKED``);
6. the applicable offer (``pricing.services.offers``) and ``create_pack_commercial_snapshot`` (ISSUED);

then the payload (``build_quotation_payload`` with the energy, savings, subsidy and finance engines, the ContentRelease
and the company master) with one sub-payload per alternative, the commercial freeze and the issued document. Nothing
here writes: :mod:`quotations.services.quotations` persists what :func:`run` returns.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any

from django.conf import settings

from core.errors import Conflict, DomainError
from engines._jscompat import JsError, json_numbers
from engines.bom_builder import BomBuildError, build_bom, get_alternatives
from engines.bom_domain import CATEGORY_TO_ROLE, Role, attempt_lock, create_project_bom, remove_role, role_for_line, set_component
from engines.energy import EnergyConfig, EnergyInputs, calculate_energy_profile
from engines.engineering_checker import CheckResult, CheckStatus, Severity, check_project_bom
from engines.finance import FinanceConfig, resolve_finance
from engines.frozen import deep_freeze, thaw
from engines.gate import derive_subsidy_treatment, evaluate_generation_gate
from engines.offers import calculate_offer_amount
from engines.pack_config import ROOF_TO_STRUCTURE_KEY
from engines.pack_pricing import kw_of, price_pack, structure_material, tube_rate_for
from engines.package_registry import resolve_package_architecture
from engines.quotation_payload import (
    CAPABILITY_COST_VIEW,
    alternative_entry,
    build_commercial_snapshot,
    build_quotation_payload,
    component_attributes_for,
    create_pack_commercial_snapshot,
    issued_document,
    normalize_renderer_pinning,
    panel_unresolved_subsidy_result,
    resolve_panel_dcr_type,
    with_alternatives,
)
from engines.savings import SavingsConfig, SavingsInputs, calculate_savings
from engines.subsidy import SubsidyConfig, SubsidyInputs, calculate_subsidy
from quotations.services import content as content_services
from quotations.services.common import ENGINE_SYSTEM, ENGINE_TIER, PLATFORM_TIER, TIER_ORDER, iso, js_number

ENGINE_ERRORS = (BomBuildError, JsError)
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates" / "documents" / "quotation"
RENDERER_NAME = "flarize-quotation-v2"
RENDERER_VERSION = "quotations.artwork.1"
SALES_CATEGORIES = ("panel", "inverter")
RESERVED_BATTERY_ROLES = (Role.BATTERY.value, Role.BATTERY_PROTECTION.value, Role.BATTERY_CABLE.value)


class TierFailure(Exception):
    """One tier could not be priced: ``code`` (Flarize's orchestration code), ``message``, ``detail``."""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class Request:
    """What a version asks for, in the engine vocabulary (``ongrid``/``value``/``3``/``1P``)."""

    system: str
    size: str
    tier: str
    phase: str
    battery: int | None
    future: str | None
    roof: str
    distance_km: Decimal
    vehicle: str | None
    tier_selections: dict
    offer_code: str
    subsidy_type: str
    ghs_houses: int | None
    language: str
    appliance_rows: list | None
    validity_override_days: int | None
    discount_total: Decimal = Decimal("0")

    @classmethod
    def from_version(cls, version) -> Request:
        selections = version.selections or {}
        return cls(
            system=ENGINE_SYSTEM[version.system_type],
            size=version.size_key,
            tier=ENGINE_TIER[version.tier],
            phase=version.phase,
            battery=int(version.battery_config) if version.battery_config != "" else None,
            future=version.future_size_key or None,
            roof=version.roof_type,
            distance_km=version.distance_km,
            vehicle=version.vehicle_type or None,
            tier_selections=selections.get("tier_selections") or {},
            offer_code=selections.get("offer_code") or "",
            subsidy_type=version.subsidy_type,
            ghs_houses=version.ghs_houses,
            language=version.language,
            appliance_rows=selections.get("appliance_rows"),
            validity_override_days=selections.get("validity_override_days"),
            discount_total=version.discount_total or Decimal("0"),
        )

    @property
    def size_kw(self) -> Decimal:
        return Decimal("".join(ch for ch in self.size if ch.isdigit() or ch == "."))


# ── the release context ────────────────────────────────────────────────────────────────────────────────────────────


class ReleaseContext:
    """What pricing a quotation reads from one PackRelease: its configuration, pins, priced catalog and engine configs."""

    def __init__(self, pack_release) -> None:
        self.pack_release = pack_release
        self.price_release = pack_release.price_release
        self.config_version = pack_release.config_version

    @cached_property
    def engine(self):
        from packs.services import context

        return context.engine_context(self.price_release)

    @cached_property
    def catalog(self) -> dict:
        """The engines' catalog as Flarize's ``catalog.json`` had it: structure items carry their ``tubeSize`` /
        ``structureRole`` / ``specification`` (``catalog_structure_spec``; ``applyRoofStructure`` maps the roof's
        primary tube to a catalog item by it) and panels their ``panelType`` (DCR / NON_DCR, from ``is_dcr``; the
        payload prints it as the panel's technology)."""
        from catalog.models import StructureSpec

        catalog = self.engine.catalog
        specs = {spec.component.sku: spec for spec in StructureSpec.objects.select_related("component").filter(component__deleted_at__isnull=True)}
        for item in ((catalog.get("categories") or {}).get("structure_material") or {}).get("items") or []:
            spec = specs.get(item.get("id"))
            if spec is None:
                continue
            for key, value in (("tubeSize", spec.tube_size), ("structureRole", spec.structure_role), ("specification", spec.specification)):
                if value and key not in item:
                    item[key] = value
        for item in ((catalog.get("categories") or {}).get("panel") or {}).get("items") or []:
            if isinstance(item.get("dcr"), bool) and "panelType" not in item:
                item["panelType"] = "DCR" if item["dcr"] else "NON_DCR"
        return catalog

    @property
    def battery_master(self) -> dict:
        return self.engine.battery_master

    @cached_property
    def config(self) -> dict:
        from packs.services import engine

        return engine.pricing_config(self.config_version.config, (self.price_release.payload or {}).get("market_rates_by_key") or {})

    @cached_property
    def pins(self) -> dict:
        from packs.services.context import version_pins

        return version_pins(self.config_version)

    @cached_property
    def rule_set_row(self):
        from engineering.services import rule_sets

        return rule_sets.active_rule_set()

    @cached_property
    def rule_set(self):
        from engineering.services import rule_sets

        return rule_sets.engine_rule_set(self.rule_set_row)

    @property
    def catalog_version(self) -> str:
        return f"price-release@{self.price_release.number}"

    @property
    def config_number(self) -> int:
        return self.config_version.number

    def cost_document(self, key: str):
        return ((self.price_release.payload or {}).get("cost_config") or {}).get(key)

    def release_pack(self, request: Request, tier: str):
        from packs.models import ReleasePack

        return ReleasePack.objects.filter(
            release=self.pack_release,
            system_type=request.system.upper(),
            tier=PLATFORM_TIER[tier],
            size_key=request.size,
            phase=request.phase,
            battery_config="" if request.battery is None else str(request.battery),
            future_size_key=request.future or "",
        ).first()

    def spec(self, request: Request, tier: str):
        from packs.services import engine

        return engine.PackSpec(request.system, request.size, tier, request.phase, request.future or "", request.battery)

    def registry(self, request: Request, tier: str) -> dict:
        from packs.services import engine

        spec = self.spec(request, tier)
        return engine.pins_registry(spec, self.pins.get(spec.key) or [])


# ── one tier ───────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class TierResult:
    tier: str
    release_pack: Any
    bom: dict
    lock: dict
    check: CheckResult
    pack_pricing: dict
    material_list: list
    offer: Any
    offer_amount: Decimal
    commercial_snapshot: dict
    profile_key: str | None
    architecture: str | None
    engineering: dict
    snapshot_id: str
    bom_snapshot_id: str


def _build_request(request: Request, tier: str, selections: dict | None) -> dict:
    config = {"systemType": request.system, "size": request.size, "tier": tier, "phase": request.phase, "configSource": "approved", "actorRole": "PROJECT_HEAD"}
    if request.future:
        config["futureSystemSize"] = request.future
    if request.battery is not None:
        config["batteryQuantity"] = request.battery
    if selections:
        config["selections"] = dict(selections)
    return config


def tier_selections(request: Request, tier: str) -> dict:
    """``resolveSalesSelections``' accepted shapes: ``{tier: {category: sku}}`` or one flat ``{category: sku}``."""
    raw = request.tier_selections or {}
    nested = any(key in raw for key in TIER_ORDER)
    chosen = raw.get(tier) or {} if nested else raw
    return {category: sku for category, sku in (chosen or {}).items() if sku not in (None, "")}


def approved_alternatives(ctx: ReleaseContext, request: Request, tier: str) -> dict:
    """The Sales-swappable slots of the pack and their approved alternatives (Sales rules, least privilege)."""
    config = {"systemType": request.system, "size": request.size, "tier": tier, "phase": request.phase, "actorRole": "SALES", "futureSystemSize": request.future}
    if request.battery is not None:
        config["batteryQuantity"] = request.battery
    try:
        return get_alternatives(config, catalog=ctx.catalog, registry=ctx.registry(request, tier), pack_config=ctx.config_version.config).get("alternatives") or {}
    except ENGINE_ERRORS as exc:
        raise TierFailure("ALTERNATIVES_UNAVAILABLE", f"Approved alternatives could not be resolved for {request.system}/{request.size}/{tier}: {exc}") from None


def validate_selections(ctx: ReleaseContext, request: Request, tier: str) -> dict:
    selections = tier_selections(request, tier)
    if not selections:
        return {}
    approved = approved_alternatives(ctx, request, tier)
    allowed = list(approved)
    for category, sku in selections.items():
        if category not in allowed:
            raise TierFailure(
                "SELECTION_NOT_PERMITTED",
                f'Sales may not configure "{category}". Only {" / ".join(allowed) or "nothing"} are Sales-selectable for this pack.',
                {"category": category, "componentId": sku, "allowed": allowed},
            )
        if not isinstance(sku, str):
            raise TierFailure("MISSING_INPUT", f"Selection for {category} must be a component id string.", {"category": category})
        ids = [item if isinstance(item, str) else (item or {}).get("id") for item in approved.get(category) or []]
        if sku not in ids:
            raise TierFailure(
                "SELECTION_NOT_APPROVED",
                f'"{sku}" is not an approved {category} alternative for the {tier} package. Approved: {", ".join(filter(None, ids)) or "none"}.',
                {"category": category, "componentId": sku, "tier": tier, "approved": [i for i in ids if i]},
            )
    return selections


def _project_lines(bom: dict) -> list[dict]:
    """The catalog-identified lines of a built BOM as project-BOM lines (fixed consumables and structure excluded)."""
    lines = []
    for line in bom.get("lines") or []:
        if not line.get("componentId") or line.get("category") == "fixed" or line.get("isStructure"):
            continue
        role = role_for_line({"category": line.get("category"), "itemId": line.get("componentId")})
        lines.append({"role": role.value, "componentId": line["componentId"], "quantity": line.get("qty")})
    lines.sort(key=lambda line: line["role"])
    return lines


def _structure_line(ctx: ReleaseContext, request: Request, tier: str) -> dict | None:
    """``applyRoofStructure``: the primary tube of the roof's structure pack, as its catalog ``structure_material``."""
    template = (ctx.config.get("structureTemplates") or {}).get(ROOF_TO_STRUCTURE_KEY.get(request.roof, ""))
    if not template:
        return None
    kw = kw_of(request.future or request.size)
    material = structure_material(template, kw, tube_rate_for(ctx.config.get("costs"), tier))
    tube = next((line for line in material.get("lines") or [] if line.get("isTube") and (line.get("qty") or 0) > 0), None)
    if tube is None:
        return None

    def norm(value) -> str:
        return str(value or "").lower().replace("×", "x").replace(" ", "")

    items = ((ctx.catalog.get("categories") or {}).get("structure_material") or {}).get("items") or []
    item = next((item for item in items if item.get("tubeSize") and norm(item["tubeSize"]) == norm(tube.get("tubeSize"))), None)
    if item is None:
        return None
    return {"componentId": item["id"], "quantity": tube["qty"], "reason": f"Structure pack {request.roof} {js_number(Decimal(str(kw)))} kW — primary tube {tube.get('tubeSize')} × {tube['qty']}"}


def run_tier(ctx: ReleaseContext, request: Request, tier: str, *, at: str, actor_id: str, ids: dict, offers) -> TierResult:
    release_pack = ctx.release_pack(request, tier)
    if release_pack is None:
        raise TierFailure(
            "NO_APPROVED_PACKAGE",
            f"No released pack for {request.system} {request.size} kW {tier} {request.phase} in PackRelease #{ctx.pack_release.number}.",
            {"tier": tier, "systemType": request.system, "size": request.size, "phase": request.phase},
        )
    selections = validate_selections(ctx, request, tier)
    registry = ctx.registry(request, tier)
    pack_config = ctx.config_version.config
    try:
        default_bom = build_bom(_build_request(request, tier, None), catalog=ctx.catalog, registry=registry, pack_config=pack_config)
        full = build_bom(_build_request(request, tier, selections), catalog=ctx.catalog, registry=registry, pack_config=pack_config) if selections else default_bom
    except ENGINE_ERRORS as exc:
        raise TierFailure("PACK_BUILD_REFUSED", f"The {tier} pack BOM could not be built: {exc}", {"tier": tier}) from None

    profile_key = (default_bom.get("systemConfig") or {}).get("profileKey")
    architecture = resolve_package_architecture({"profileKey": profile_key}, ctx.catalog)["architecture"]
    project = create_project_bom(
        project_id=ids["project"],
        package_id=release_pack.key,
        architecture=architecture,
        sys_type=request.system,
        tier=tier,
        size_kw=js_number(request.size_kw),
        phase=request.phase,
        lines=_project_lines(default_bom),
        created_by=actor_id,
        created_at=at,
    )
    if request.system == "hybrid" and request.battery == 0:
        for role in RESERVED_BATTERY_ROLES:
            if any(line["role"] == role for line in project["packageLines"]):
                project = remove_role(project, role=role, selected_by=actor_id, at=at, reason="Project battery quantity 0 — PROJECT_HEAD project configuration (B.6)")
    for category, sku in selections.items():
        role = CATEGORY_TO_ROLE.get(category)
        qty = next((line.get("qty") for line in full.get("lines") or [] if line.get("category") == category), None)
        project = set_component(project, role=role, component_id=sku, quantity=qty, selected_by=actor_id, at=at, reason=f"Sales-selected approved alternative ({category})")
    structure = _structure_line(ctx, request, tier)
    if structure is None:
        raise TierFailure(
            "STRUCTURE_PACK_UNRESOLVED",
            f"No structure pack line could be placed for {request.roof} roof: the structure template's tube sections must map to catalog structure_material items.",
            {"roofType": request.roof, "owner": "PROJECT_HEAD"},
        )
    project = set_component(project, role=Role.STRUCTURE.value, component_id=structure["componentId"], quantity=structure["quantity"], selected_by=actor_id, at=at, reason=structure["reason"])

    check = check_project_bom(bom=project, catalog=ctx.catalog, battery_master=ctx.battery_master, at=at, catalog_version=ctx.catalog_version, rule_set=ctx.rule_set)
    acknowledgements = [
        {
            "ruleId": finding.rule_id,
            "acknowledgedBy": actor_id,
            "acknowledgedAt": at,
            "reason": f"Auto-acknowledged: {tier} package {release_pack.key} is released (PackRelease #{ctx.pack_release.number})",
        }
        for finding in check.findings
        if finding.severity == Severity.WARN
    ]
    outcome = attempt_lock(
        bom=project,
        catalog=ctx.catalog,
        battery_master=ctx.battery_master,
        catalog_version=ctx.catalog_version,
        locked_by=actor_id,
        locked_at=at,
        acknowledgements=acknowledgements,
        rule_set=ctx.rule_set,
    )
    if not outcome.locked:
        raise TierFailure("ENGINEERING_BLOCKED", f"BOM lock failed: {outcome.reason}", {"tier": tier, "missingAcknowledgements": [dict(item) for item in outcome.missing_acknowledgements]})

    priced = price_pack(
        config=ctx.config,
        system_type=request.system,
        size=request.size,
        tier=tier,
        roof_type=request.roof,
        distance_km=json_numbers(request.distance_km),
        vehicle_type=request.vehicle,
        battery_config=(full.get("systemConfig") or {}).get("batteryQuantity") or 0,
        future_system_size=request.future,
        lines=full.get("lines"),
        priced_at=at,
    )
    if priced.get("status") != "COMPLETE":
        raise TierFailure(
            "PACK_PRICING_BLOCKED",
            f"Pack pricing blocked: {' | '.join(error.get('message', '') for error in priced.get('errors') or [])}",
            {"tier": tier, "errors": priced.get("errors") or [], "owner": "PROJECT_HEAD"},
        )
    material_list = [
        {
            "category": line.get("category"),
            "componentId": line.get("componentId"),
            "name": line.get("name"),
            "qty": line.get("qty"),
            "isVariable": bool(line.get("isVariable")),
            "selectionMethod": line.get("selectionMethod"),
        }
        for line in full.get("lines") or []
    ] + [
        {"category": "structure", "componentId": None, "name": line.get("name"), "qty": line.get("qty"), "isVariable": False, "selectionMethod": "STRUCTURE_PACK"}
        for line in (priced.get("structureMaterial") or {}).get("lines") or []
    ]
    selling_before_gst = priced["customer"]["sellingPriceBeforeGST"]
    offer, engine_offer, offer_amount = _offer(offers, request, tier, selling_before_gst)
    snapshot = create_pack_commercial_snapshot(
        snapshot_id=ids["commercial"],
        project_id=ids["project"],
        bom_snapshot_id=ids["bom"],
        pack_pricing=priced,
        config_version=ctx.config_number,
        material_list=material_list,
        cost_result=None,
        issued_by=actor_id,
        issued_at=at,
        offer={**engine_offer, "offerAmount": js_number(offer_amount)} if engine_offer else None,
        catalog_version=ctx.catalog_version,
    )
    engineering = {"status": check.status.value, "counts": check.counts.as_dict(), "checkedAt": at}
    return TierResult(
        tier=tier,
        release_pack=release_pack,
        bom=full,
        lock=outcome.snapshot,
        check=check,
        pack_pricing=priced,
        material_list=material_list,
        offer=offer,
        offer_amount=offer_amount,
        commercial_snapshot=snapshot,
        profile_key=profile_key,
        architecture=architecture,
        engineering=engineering,
        snapshot_id=ids["commercial"],
        bom_snapshot_id=ids["bom"],
    )


def _offer(offers, request: Request, tier: str, selling_before_gst) -> tuple[Any, dict | None, Decimal]:
    """The explicit ACTIVE offer (by code) or the best applicable one; its amount on the pre-GST price (D-4)."""
    from pricing.services import offers as offer_services

    offer = None
    if request.offer_code:
        offer = next((item for item in offers if item.code.lower() == request.offer_code.lower()), None)
    if offer is None:
        offer = offer_services.applicable_offer(system_type=request.system, tier=tier, size_key=request.size)
    if offer is None:
        return None, None, Decimal("0")
    engine_offer = offer_services.as_engine_offer(offer)
    amount = Decimal(repr(calculate_offer_amount(engine_offer, selling_before_gst))).quantize(Decimal("0.01"))
    return offer, engine_offer, amount


# ── the whole version ──────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    at: str
    primary: TierResult
    tiers: dict[str, TierResult]
    unavailable: list[dict]
    payload: dict
    gate: Any
    subsidy_result: Any
    document: dict | None = None
    freeze: dict | None = None
    validity: dict | None = None
    extras: dict = field(default_factory=dict)


def ids_for(version_uid, tier: str) -> dict:
    base = f"{version_uid}:{tier}"
    return {"project": f"PRJ-{base}", "bom": f"SNAP-{base}", "commercial": f"CS-{base}"}


def run_tiers(ctx: ReleaseContext, request: Request, *, at: str, actor_id: str, version_uid) -> tuple[dict[str, TierResult], list[dict]]:
    """Every tier; the version's own tier must price, an alternative that cannot is reported as unavailable."""
    from pricing.models import Offer, OfferStatus

    offers = list(Offer.objects.filter(status=OfferStatus.ACTIVE))
    if request.offer_code and not any(offer.code.lower() == request.offer_code.lower() for offer in offers):
        raise DomainError("offer_not_active", f"Offer {request.offer_code!r} is not an ACTIVE offer.", errors={"offer_code": ["Not an active offer."]})
    results: dict[str, TierResult] = {}
    unavailable = []
    for tier in TIER_ORDER:
        try:
            results[tier] = run_tier(ctx, request, tier, at=at, actor_id=actor_id, ids=ids_for(version_uid, tier), offers=offers)
        except TierFailure as failure:
            if tier == request.tier:
                raise Conflict(failure.code.lower(), f"Selected pack ({tier}): {failure.message}", errors={"detail": [str(failure.detail)]}) from None
            unavailable.append({"tier": tier, "code": failure.code, "message": failure.message})
    return results, unavailable


def _config(document, parser):
    return parser.from_json(json_numbers(document)) if document else None


def energy_profile(ctx: ReleaseContext, customer: dict, system: dict):
    """``resolveEnergyProfileResult``: nothing without a positive bill; the connection phase defaults to single."""
    document = ctx.cost_document("energy.config")
    bill = customer.get("currentBillAmount")
    if not document or bill is None or Decimal(str(bill)) <= 0:
        return None, None
    cycle = str(customer.get("currentBillCycle") or "").lower().replace(" ", "").replace("-", "").replace("_", "")
    config = _config(document, EnergyConfig)
    size = system.get("systemSizeKw")
    inputs = EnergyInputs(
        bill_amount=Decimal(str(bill)),
        billing_cycle="bimonthly" if "bimonthly" in cycle else "monthly",
        phase=customer.get("connectionPhase") or customer.get("phase") or "single",
        system_size_kw=Decimal(str(size)) if size not in (None, 0) and Decimal(str(size)) > 0 else None,
    )
    return calculate_energy_profile(inputs, config), config


def savings_result(ctx: ReleaseContext, profile, config, snapshot: dict, customer: dict):
    if profile is None or not profile.available:
        return None
    pricing = (snapshot or {}).get("pricing") or {}
    total = pricing.get("customerTotalIncludingGST") if (snapshot or {}).get("status") == "ISSUED" else None
    region = config.regions.get(profile.region_id or config.default_region) if config else None
    savings_config = _config(ctx.cost_document("savings.config") or {}, SavingsConfig)
    return calculate_savings(
        SavingsInputs(
            energy_profile=profile,
            customer_total_including_gst=Decimal(str(total)) if total is not None else None,
            current_bill_amount=Decimal(str(customer["currentBillAmount"])) if customer.get("currentBillAmount") is not None else None,
            current_bill_cycle=customer.get("currentBillCycle") or "monthly",
        ),
        region=region,
        config=savings_config,
    )


def subsidy_result(ctx: ReleaseContext, request: Request, system: dict, lock: dict, attributes: dict, at: str):
    """``resolveSubsidyResult``: none without a subsidy type; the panel DCR status from the locked BOM (never assumed)."""
    if not request.subsidy_type or request.subsidy_type == "none":
        return None
    document = ctx.cost_document("subsidy.config")
    if not document or not system.get("systemSizeKw"):
        return None
    panel_type = resolve_panel_dcr_type(lock, attributes)
    if panel_type is None:
        return panel_unresolved_subsidy_result(request.subsidy_type)
    return calculate_subsidy(
        SubsidyInputs(
            system_size_kw=Decimal(str(system["systemSizeKw"])),
            subsidy_type=request.subsidy_type,
            ghs_houses=request.ghs_houses if request.ghs_houses is not None else 1,
            panel_type=panel_type,
            connection_type="domestic",
        ),
        _config(document, SubsidyConfig),
        calculated_at=at,
    )


def finance_result(ctx: ReleaseContext, snapshot: dict, subsidy, at: str):
    document = ctx.cost_document("finance.config")
    pricing = (snapshot or {}).get("pricing") or {}
    gross = pricing.get("customerTotalIncludingGST") if (snapshot or {}).get("status") == "ISSUED" else None
    if not document or gross is None:
        return None
    subsidy_value = subsidy.as_dict() if hasattr(subsidy, "as_dict") else subsidy
    return resolve_finance(Decimal(str(gross)), subsidy_value, _config(document, FinanceConfig), calculated_at=at)


def _as_js(result):
    return result.as_dict() if hasattr(result, "as_dict") else result


def with_reductions(snapshot: dict, *, offer_amount: Decimal, discount: Decimal, offer) -> dict:
    """D-4: an offer (and an approved discount) change the printed price — pinned on the snapshot's ``pricing.discount``
    so the payload prints it (``customerTotalIncludingGST`` stays the pack price; ``customerPayable`` is the final price)."""
    if not offer_amount and not discount:
        return snapshot
    document = thaw(snapshot)
    pricing = document["pricing"]
    total = Decimal(str(pricing["customerTotalIncludingGST"]))
    reduction = offer_amount + discount
    pricing["discount"] = {
        "offerCode": offer.code if offer else None,
        "offerName": offer.name if offer else None,
        "offerAmount": js_number(offer_amount),
        "approvedDiscount": js_number(discount),
        "totalReduction": js_number(reduction),
        "customerPayable": js_number(max(Decimal("0"), total - reduction)),
        "basis": "D-4: offers and approved discounts reduce the printed customer price (after GST).",
    }
    return deep_freeze(document)


def renderer_pin(at: str) -> dict:
    """The pin of the artwork templates that render this document (hashes of the HTML templates, Flarize R1/R2)."""
    files = sorted(TEMPLATE_DIR.glob("**/*.html"))
    bundle = hashlib.sha256()
    for path in files:
        bundle.update(path.relative_to(TEMPLATE_DIR).as_posix().encode())
        bundle.update(path.read_bytes())
    templates = hashlib.sha256(b"".join(path.read_bytes() for path in files if path.name in ("en.html", "ml.html"))).hexdigest()
    manifest = hashlib.sha256("\n".join(path.relative_to(TEMPLATE_DIR).as_posix() for path in files).encode()).hexdigest()
    return normalize_renderer_pinning(
        {"name": RENDERER_NAME, "version": "v1", "bundleSha256": bundle.hexdigest(), "templateSha256": templates, "manifestSha256": manifest, "rendererVersion": RENDERER_VERSION},
        at,
    )


def build(
    ctx: ReleaseContext,
    request: Request,
    *,
    quotation: dict,
    customer: dict,
    at: str,
    actor_id: str,
    actor_role: str,
    version_uid,
    version_number: int,
    content_release=None,
    company: dict | None = None,
    branding_store: dict | None = None,
    policy: dict | None = None,
    freeze: bool = False,
) -> Outcome:
    """Price every tier and assemble the payload (``freeze`` adds the commercial freeze and the issued document)."""
    tiers, unavailable = run_tiers(ctx, request, at=at, actor_id=actor_id, version_uid=version_uid)
    primary = tiers[request.tier]
    release = (content_release.release_payload or {}) if content_release is not None else {}
    document_content = {"version": content_release.number, "content": content_release.language_payload} if content_release is not None else None
    tier_names = content_services.tier_names_for(release.get("tierDisplayNames"), request.system.upper())
    inclusion_matrix = release.get("inclusionMatrix")
    testimonials = content_services.testimonials_content(release.get("testimonials"), request.language) if content_release is not None else None
    on_day = dt.datetime.fromisoformat(at.replace("Z", "+00:00")).date()
    campaign = content_services.campaign_result(content_services.active_campaign(release.get("campaigns") or [], on_day), request.language)

    def payload_for(result: TierResult, system: dict, quotation_section: dict) -> tuple[dict, Any, Any]:
        snapshot = with_reductions(result.commercial_snapshot, offer_amount=result.offer_amount, discount=request.discount_total if result is primary else Decimal("0"), offer=result.offer)
        attributes = component_attributes_for(ctx.catalog, [line.get("componentId") for line in result.lock.get("lines") or []])
        subsidy = subsidy_result(ctx, request, system, result.lock, attributes, at)
        treatment = derive_subsidy_treatment({"subsidyTreatment": None if request.subsidy_type and request.subsidy_type != "none" else "NOT_QUOTED"}, _as_js(subsidy))
        profile, energy_config = energy_profile(ctx, customer, system)
        savings = savings_result(ctx, profile, energy_config, snapshot, customer)
        finance = finance_result(ctx, snapshot, subsidy, at)
        payload = build_quotation_payload(
            quotation=quotation_section,
            customer=customer,
            system=system,
            bom_snapshot=result.lock,
            commercial_snapshot=snapshot,
            engineering=primary.engineering,  # alternatives carry the record's (primary) verdict, as buildAlternativePayloads
            component_attributes=attributes,
            subsidy_treatment=treatment,
            capabilities=[CAPABILITY_COST_VIEW],
            savings_result=_as_js(savings),
            subsidy_result=_as_js(subsidy),
            financing_result=_as_js(finance),
            energy_profile_result=_as_js(profile),
            campaign_result=campaign,
            company_profile=company,
            tier_display_names=tier_names,
            inclusion_matrix=inclusion_matrix,
            testimonials_content=testimonials,
            document_content=document_content,
            appliance_rows=request.appliance_rows or None,
        )
        return payload, subsidy, treatment

    base_system = {"systemType": request.system, "systemSizeKw": js_number(request.size_kw), "phase": request.phase, "packageTier": request.tier}
    primary_quotation = {**quotation, "quotationVersion": version_number, "status": "ISSUED" if freeze else quotation.get("status")}
    payload, subsidy, treatment = payload_for(primary, base_system, primary_quotation)
    gate = evaluate_generation_gate(
        customer=customer,
        system=base_system,
        bom_snapshot=primary.lock,
        engineering=primary.engineering,
        commercial_snapshot=primary.commercial_snapshot,
        subsidy_treatment=treatment,
        quotation=primary_quotation,
    )
    alternatives = []
    # ``buildAlternativePayloads`` reads the record before issue marks it ISSUED: alternatives carry its DRAFT status.
    alternative_quotation = {key: primary_quotation.get(key) for key in ("quotationNumber", "quotationVersion", "quotationDate", "validUntil", "proposalBy", "salespersonId", "quotationLanguage")}
    alternative_quotation["status"] = quotation.get("status")
    for tier in TIER_ORDER:
        if tier == request.tier or tier not in tiers:
            continue
        system = {**base_system, "packageTier": tier}
        alternative, _, _ = payload_for(tiers[tier], system, alternative_quotation)
        option = {"tier": tier, "bomSnapshotId": tiers[tier].bom_snapshot_id, "commercialSnapshotId": tiers[tier].snapshot_id, "system": system}
        alternatives.append(alternative_entry(len(alternatives), option, system, alternative))
    payload = with_alternatives(payload, alternatives)
    outcome = Outcome(at=at, primary=primary, tiers=tiers, unavailable=unavailable, payload=payload, gate=gate, subsidy_result=subsidy)
    if not freeze:
        return outcome

    pin = renderer_pin(at)
    transport = ctx.config.get("transportConfig") or {}
    vehicles = transport.get("vehicles") or []
    frozen = build_commercial_snapshot(
        issued_at=at,
        bom={
            "engineering": {"status": primary.check.status.value, "blockers": [finding.as_dict() for finding in primary.check.findings if finding.severity == Severity.BLOCK]},
            "systemConfig": {
                "systemType": request.system,
                "size": request.size,
                "phase": request.phase,
                "tier": request.tier,
                "profileKey": primary.profile_key,
                "batteryQuantity": request.battery,
                "batterySource": "PROJECT_HEAD_OVERRIDE" if request.battery is not None else None,
                "batteryIncluded": request.system == "hybrid",
                "roofType": request.roof,
                "variant": "FUTURE_READY" if request.future else "STANDARD",
                "futureSystemSize": request.future,
            },
        },
        project={"distanceKm": json_numbers(request.distance_km), "vehicleType": request.vehicle or (vehicles[0]["vehicleType"] if len(vehicles) == 1 else None)},
        transportation_config={
            "version": f"pack-config@v{ctx.config_number}",
            "source": "PACK_CONFIG_TRANSPORT",
            "isDemo": False,
            "distanceBasis": "ONE_WAY",
            "baseDistanceKm": transport.get("baseDistanceKm"),
            "vehicles": vehicles,
        },
        commercial_snapshot=primary.commercial_snapshot,
        subsidy_result=_as_js(subsidy),
        branding_store=branding_store,
        branding_overrides={},
        policy=policy,
        validity_override_days=request.validity_override_days,
        renderer_pin=pin,
        package_profile={"profileKey": primary.profile_key},
        production_mode=bool(getattr(settings, "QUOTATIONS_PRODUCTION_MODE", True)),
    )
    outcome.freeze = frozen
    outcome.validity = frozen.get("validity")
    outcome.document = issued_document(
        quotation_id=quotation.get("quotationId"),
        version=version_number,
        issued_by=actor_id,
        issued_by_role=actor_role,
        issued_at=at,
        commercial_snapshot_id=primary.snapshot_id,
        bom_snapshot_id=primary.bom_snapshot_id,
        cms_page_versions=None,
        renderer_pinning=pin,
        payload=payload,
        snapshot=frozen,
    )
    return outcome


def now_iso() -> str:
    return iso()


def check_is_blocked(check: CheckResult) -> bool:
    return check.status == CheckStatus.BLOCKED
