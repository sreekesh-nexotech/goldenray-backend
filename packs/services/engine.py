"""The packs services' glue to the pure engines (``bom_builder``, ``pack_pricing``, ``pack_config``,
``engineering_checker``, ``package_registry``), with the Flarize semantics the parity tests pin.

* :func:`enumerate_packs` — every pack a configuration offers: for each template system type (``upgrade`` has its own
  model) × size × tier × {standard, the future-ready pairs of the same phase} × the hybrid template's battery
  configurations (the enumeration of Flarize ``server-pack-publish.js``, battery configurations added because each is
  priced by its own market-rate key);
* :func:`build` — ``build_bom`` as the Project Head (no Sales restrictions) with the pack's pins as its registry
  package (the platform replacement of ``packages.proposed.json``);
* :func:`check` — the engineering checker at template scope on the components the BOM resolves to, exactly as
  ``publishApprovedPacks`` runs ``runPackageChecker`` (``componentsFromBom`` → registry lines → architecture from the
  profile);
* :func:`price` — ``price_pack`` for the FLAT roof with no transport extra: the pack's base price.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from engines._jscompat import JsError, json_numbers
from engines.bom_builder import BomBuildError, build_bom, load_catalog
from engines.engineering_checker import CheckResult, check_project_bom
from engines.flarize_rbac import allow_all
from engines.pack_config import market_rate_key
from engines.pack_pricing import kw_of, price_pack, structure_material, tube_rate_for
from engines.package_registry import run_package_checker
from packs.services.common import pack_key

EngineError = (BomBuildError, JsError)
BATTERY_WORDS = {0: "no battery", 1: "1 battery", 2: "2 batteries"}


@dataclass(frozen=True)
class PackSpec:
    system: str  # engine vocabulary: ongrid / hybrid
    size: str
    tier: str  # base / value / premium
    phase: str
    future: str = ""
    battery: int | None = None
    sort_order: int = 0

    @property
    def system_size(self) -> str:
        return self.future or self.size

    @property
    def battery_config(self) -> str:
        return "" if self.battery is None else str(self.battery)

    @property
    def key(self) -> str:
        return pack_key(self.system, self.tier, self.size, self.battery_config, self.future)

    @property
    def market_rate_key(self) -> str:
        return market_rate_key(system_type=self.system, tier=self.tier, battery_config=self.battery if self.battery is not None else 0, future_system_size=self.future or None)


def _phase_of(template: dict, size: str) -> str:
    return "3P" if size in (template.get("threePhase") or []) else "1P"


def enumerate_packs(config: dict) -> list[PackSpec]:
    specs: list[PackSpec] = []
    pairs = ((config.get("futureUpgrade") or {}).get("pairs")) or []
    for system, template in (config.get("bomTemplates") or {}).items():
        if system == "upgrade" or system not in ("ongrid", "hybrid") or not isinstance(template, dict):
            continue
        sizes = template.get("sizes") or {}
        for size in sizes:
            phase = _phase_of(template, size)
            variants = [""]
            for pair in pairs:
                if (pair.get("systemType") or system) != system or str(pair.get("panelSize")) != str(size):
                    continue
                future = str(pair.get("systemSize"))
                if future not in sizes or _phase_of(template, future) != phase:
                    continue
                variants.append(future)
            batteries = [int(value) for value in (template.get("batteryConfigs") or ["0", "1", "2"])] if system == "hybrid" else [None]
            for tier in template.get("tiers") or ["base", "value", "premium"]:
                for future in variants:
                    for battery in batteries:
                        specs.append(PackSpec(system, str(size), tier, phase, future, battery, len(specs)))
    return specs


def display_name(spec: PackSpec, config: dict) -> str:
    sizes = ((config.get("bomTemplates") or {}).get(spec.system) or {}).get("sizes") or {}
    name = f"{'Hybrid' if spec.system == 'hybrid' else 'On-grid'} {sizes.get(spec.size) or spec.size} {spec.tier.capitalize()}"
    if spec.future:
        name += f" (future-ready {sizes.get(spec.future) or spec.future})"
    if spec.battery is not None:
        name += f" · {BATTERY_WORDS.get(spec.battery, f'{spec.battery} batteries')}"
    return name[:120]


def pins_registry(spec: PackSpec, pins: list[dict]) -> dict:
    """``{"packages": [...]}`` with one package — this pack's pins — matched by ``build_bom``'s registry lookup."""
    if not pins:
        return {"packages": []}
    return {
        "packages": [
            {
                "systemType": spec.system,
                "size": spec.system_size,
                "tier": spec.tier,
                "phase": spec.phase,
                "components": [
                    {
                        "role": pin["slot_key"],
                        "componentId": pin["sku"],
                        "derivedBy": "explicit" if pin.get("authoritative", True) else "fallback",
                        "approvedAlternates": list(pin.get("alternates") or []),
                    }
                    for pin in pins
                ],
            }
        ]
    }


def build(spec: PackSpec, *, config: dict, catalog: dict, pins: list[dict]) -> dict:
    """``build_bom`` for the pack; raises ``BomBuildError``/``JsError`` when the engine refuses it."""
    request = {"systemType": spec.system, "size": spec.size, "tier": spec.tier, "phase": spec.phase, "configSource": "approved", "actorRole": "PROJECT_HEAD"}
    if spec.future:
        request["futureSystemSize"] = spec.future
    if spec.battery is not None:
        request["batteryQuantity"] = spec.battery
    return build_bom(request, catalog=catalog, registry=pins_registry(spec, pins), pack_config=config)


def components_from_bom(bom: dict) -> list[dict]:
    """``server-pack-publish.componentsFromBom``: catalog-identified lines, consumables and structure excluded."""
    return [
        {"role": line["category"], "componentId": line["componentId"], "quantity": line["qty"]}
        for line in bom.get("lines") or []
        if line.get("componentId") and line.get("category") != "fixed" and not line.get("isStructure")
    ]


def checker_catalog(catalog: dict, config: dict) -> dict:
    return load_catalog(catalog, config, "approved")


def check(spec: PackSpec, bom: dict, *, checker_env: dict, rule_set, at: str) -> CheckResult:
    """The pack's verdict: ``runPackageChecker`` on the package derived from its BOM, with ``rule_set``."""
    captured: list[CheckResult] = []

    def checker(arguments: dict) -> dict:
        lines = arguments.get("lines")
        result = check_project_bom(
            bom=arguments.get("bom"),
            catalog=arguments.get("catalog"),
            battery_master=arguments.get("batteryMaster") or {},
            at=arguments.get("at"),
            catalog_version=arguments.get("catalogVersion"),
            lines=list(lines) if isinstance(lines, (list, tuple)) else None,
            template_scope=True,
            rule_set=rule_set,
        )
        captured.append(result)
        return result.as_dict()

    package = {
        "packageId": spec.key,
        "systemType": spec.system,
        "size": spec.size,
        "phase": spec.phase,
        "tier": spec.tier,
        "profileKey": (bom.get("systemConfig") or {}).get("profileKey"),
        "architecture": None,
        "components": components_from_bom(bom),
        "revisionNumber": 1,
    }
    run_package_checker({"packages": [package]}, checker_env, actor={"userId": "platform", "role": "ADMIN"}, package_id=spec.key, at=at, check_project_bom=checker, authorize=allow_all)
    return captured[0]


def pricing_config(config: dict, market_rates_by_key: dict) -> dict:
    """The approved configuration with its ``marketRates`` replaced by the PriceRelease's (the market-rate authority)."""
    effective = copy.deepcopy(config)
    effective["marketRates"] = {key: {size: json_numbers(_decimal(value)) for size, value in sizes.items()} for key, sizes in (market_rates_by_key or {}).items()}
    return effective


def _decimal(value):
    from decimal import Decimal

    return Decimal(str(value)) if value is not None else Decimal("0")


def price(spec: PackSpec, bom: dict, *, config: dict, at: str) -> dict:
    return price_pack(
        config=config,
        system_type=spec.system,
        size=spec.size,
        tier=spec.tier,
        roof_type="FLAT",
        distance_km=0,
        battery_config=(bom.get("systemConfig") or {}).get("batteryQuantity"),
        future_system_size=spec.future or None,
        lines=bom.get("lines"),
        priced_at=at,
    )


def structure_lines(spec: PackSpec, config: dict) -> list[dict]:
    """The flat-roof structure material of the pack (``structureMaterial`` over the system kW, the tier's steel rate)."""
    template = (config.get("structureTemplates") or {}).get("flatRoof")
    if not template:
        return []
    return structure_material(template, kw_of(spec.system_size), tube_rate_for(config.get("costs") or {}, spec.tier))["lines"]
