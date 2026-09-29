"""Parity: five representative quotations issued by the platform equal what the Flarize engines produced.

``quotations/tests/golden/flarize_quotations.json`` holds the result of the REAL Flarize JavaScript
(``salesOrchestrator.orchestrateThreeTiers`` in pack mode, as ``POST /api/sales/orchestrate`` runs it) for five
representative quotations — every sellable on-grid size and phase, a future-ready pack, the three roof types,
transport beyond the included km, both languages, the three subsidy types, Sales appliance rows. The platform imports
the same Flarize data through its importers, publishes PriceRelease #1 / PackRelease #1, and issues the same five
quotations through ``quotations.services``; for every tier the engine outputs must be equal:

* the pack pricing (market rate, swap deltas, roof add-on, structure material, transport extra, GST, customer total,
  the internal reference cost) and the ISSUED commercial snapshot's cost / pricing / pack domains;
* the LOCKED BOM snapshot: every line's component, role and quantity, the rules version, the verdict and the
  acknowledged warnings;
* the payload: gate report, system (tier names, badge), inclusions, BOM rows and technical specifications, pricing,
  extras, subsidy, savings, financing, consumption, energy profile, appliance usage, content (version and derived
  figures), testimonials, terms, and every alternative tier's payload; the commercial freeze (engineering, battery,
  package, transportation, commercial, subsidy, validity, pack).

Excluded by design (documented in docs/decisions/quotations.md): identifiers minted per run (quotation, project and
snapshot ids, numbers), the clock-derived ``checkedAt``/timestamps equal by construction, the company master and
branding (the platform's company profile and bank account, D-9), version labels naming the platform's releases
(``catalogVersion``, ``costConfigVersion`` …), the renderer pin (the platform's templates), the landed-cost check
(Flarize's cost engine over its procurement file; the platform's is the PackRelease landed cost, DV).
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from freezegun import freeze_time

from customers.models import Customer
from engines.frozen import jsonable
from quotations.models import BomSnapshot, CommercialSnapshot
from quotations.services import quotations as quotation_services
from quotations.tests import flarize

pytestmark = pytest.mark.django_db
AT = "2026-09-20T10:00:00.000Z"
TIERS = {"base": "BASE", "value": "VALUE", "premium": "PREMIUM"}
PAYLOAD_SECTIONS = (
    "payloadVersion",
    "variant",
    "generationGate",
    "inclusions",
    "inclusionsByTier",
    "upgrade",
    "bomSummary",
    "technicalSpecifications",
    "extras",
    "subsidy",
    "savings",
    "financing",
    "consumption",
    "applianceUsage",
    "energyProfile",
    "testimonials",
    "campaign",
    "terms",
    "versionKeys",
)


def _num(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def canon(value):
    """JSON-equal comparison form (ints/floats unified the JavaScript way)."""
    value = jsonable(value)
    if isinstance(value, dict):
        return {key: canon(item) for key, item in value.items()}
    if isinstance(value, list):
        return [canon(item) for item in value]
    return _num(value)


def _drop(value, *keys):
    return {key: item for key, item in (value or {}).items() if key not in keys}


def _pricing(section: dict) -> dict:
    out = dict(section)
    out.pop("snapshotId", None)
    if isinstance(out.get("customer"), dict):
        out["customer"] = _drop(out["customer"], "discount")
    if isinstance(out.get("internal"), dict):
        internal = dict(out["internal"])
        internal["cost"] = _drop(internal.get("cost"), "landedCostCheck", "traces")
        out["internal"] = internal
    return out


def _bom_rows(section: dict) -> dict:
    out = _drop(section, "bomSnapshotId")
    out["rows"] = [_drop(row, "componentDataVersion") for row in section.get("rows") or []]
    return out


def _testimonials(section):
    """Flarize's testimonial photos are its CMS assets (``/api/cms/assets/…``), not migrated: the platform has none."""
    if isinstance(section, dict) and isinstance(section.get("value"), dict):
        value = dict(section["value"])
        value["entries"] = [{**entry, "photoUri": None if str(entry.get("photoUri") or "").startswith("/api/cms/") else entry.get("photoUri")} for entry in value.get("entries") or []]
        return {**section, "value": value}
    return section


def check_d4_discount(mine: dict, theirs: dict, offer_amount, where: str) -> None:
    """Flarize pinned the applicable offer on the snapshot but never printed it (workflows spec J.3); D-4: the platform
    prints it as ``pricing.customer.discount`` (the customer total itself is unchanged)."""
    assert theirs["available"] is False, where
    if not offer_amount:
        assert canon(mine) == canon(theirs), f"{where}: discount"
        return
    assert mine["available"] is True, where
    assert _num(mine["value"]["offerAmount"]) == _num(offer_amount) == _num(mine["value"]["totalReduction"]), f"{where}: D-4 offer"


def compare_payload(mine: dict, theirs: dict, where: str, offer_amount=None) -> None:
    for section in PAYLOAD_SECTIONS:
        a, b = canon(mine.get(section)), canon(theirs.get(section))
        if section == "testimonials":
            b = canon(_testimonials(theirs.get(section)))
        if section in ("bomSummary", "technicalSpecifications") and isinstance(a, dict) and isinstance(b, dict):
            a, b = _bom_rows(a), _bom_rows(b)
        assert a == b, f"{where}: payload.{section}"
    assert canon(_pricing(mine["pricing"])) == canon(_pricing(theirs["pricing"])), f"{where}: payload.pricing"
    check_d4_discount(mine["pricing"]["customer"]["discount"], theirs["pricing"]["customer"]["discount"], offer_amount, where)
    assert canon(_drop(mine["system"], "source")) == canon(_drop(theirs["system"], "source")), f"{where}: payload.system"
    assert canon(_drop(mine["quotation"], "quotationNumber", "salespersonId", "proposalBy", "validUntil")) == canon(
        _drop(theirs["quotation"], "quotationNumber", "salespersonId", "proposalBy", "validUntil")
    ), f"{where}: payload.quotation"
    mine_content, theirs_content = mine["content"], theirs["content"]
    assert mine_content["available"] == theirs_content["available"], f"{where}: payload.content"
    if theirs_content["available"]:
        assert canon(mine_content["value"]["content"]) == canon(theirs_content["value"]["content"]), f"{where}: content"
        assert canon(mine_content["value"]["derived"]) == canon(theirs_content["value"]["derived"]), f"{where}: content.derived"
        assert mine_content["value"]["version"] == theirs_content["value"]["version"], f"{where}: content.version"
    assert canon(_drop(mine["engineering"], "checkedAt")) == canon(_drop(theirs["engineering"], "checkedAt")), f"{where}: payload.engineering"


def compare_snapshot(mine: dict, theirs: dict, where: str) -> None:
    assert mine["status"] == theirs["status"] == "ISSUED"
    for domain in ("pricing", "pack", "offer"):
        assert canon(mine.get(domain)) == canon(theirs.get(domain)), f"{where}: commercialSnapshot.{domain}"
    assert canon(_drop(mine["cost"], "landedCostCheck", "traces", "materialLandedCost")) == canon(_drop(theirs["cost"], "landedCostCheck", "traces", "materialLandedCost")), f"{where}: cost"
    assert canon(mine["cost"]["traces"]) == canon(theirs["cost"]["traces"]), f"{where}: cost.traces"
    for key in ("marginVersion", "gstVersion", "pricingEngineVersion", "moneyRuleVersion", "offerVersion"):
        assert mine["versions"][key] == theirs["versions"][key], f"{where}: versions.{key}"
    assert mine["pricingMode"] == theirs["pricingMode"] and mine["gstRegime"] == theirs["gstRegime"]


def compare_lock(mine: dict, theirs: dict, where: str) -> None:
    def lines(record):
        return [(line["role"], line["componentId"], _num(line["quantity"]), line["selectionMethod"], line["engineeringApprovalState"]) for line in record["lines"]]

    assert lines(mine) == lines(theirs), f"{where}: BOM lines"
    for key in ("status", "engineeringStatus", "architecture", "rulesVersion", "validationStatus"):
        assert mine[key] == theirs[key], f"{where}: bom.{key}"
    assert [ack["ruleId"] for ack in mine["acknowledgements"]] == [ack["ruleId"] for ack in theirs["acknowledgements"]], f"{where}: acknowledgements"


def compare_freeze(mine: dict, theirs: dict, where: str) -> None:
    for domain in ("engineering", "battery", "transportation", "subsidy", "pack"):
        assert canon(mine.get(domain)) == canon(theirs.get(domain)), f"{where}: freeze.{domain}"
    theirs_package = dict(theirs["package"])
    if theirs_package.get("profileKey") == theirs_package.get("tier"):
        # Flarize froze the registry package's own profileKey; its premium on-grid packages carry the bare tier name
        # ("premium") where the pack profile is "ongrid_premium" — the platform freezes the pack's profile key.
        theirs_package["profileKey"] = f"{theirs_package['systemType']}_{theirs_package['tier']}"
    assert canon(_drop(mine["package"], "profileNotes")) == canon(_drop(theirs_package, "profileNotes")), f"{where}: freeze.package"
    assert canon(_drop(mine["commercial"], "marginVersion")) == canon(_drop(theirs["commercial"], "marginVersion")), f"{where}: freeze.commercial"
    assert canon(_drop(mine["validity"], "policyVersion")) == canon(_drop(theirs["validity"], "policyVersion")), f"{where}: freeze.validity"


def _offer(case: dict, tier: str):
    return ((case["tiers"][tier]["commercialSnapshot"] or {}).get("offer") or {}).get("offerAmount")


def _customer(case: dict) -> Customer:
    raw = case["input"]["customer"]
    cycle = {"monthly": "MONTHLY", "bimonthly": "BIMONTHLY"}[raw["currentBillCycle"]]
    return Customer.objects.create(
        code=f"CUST-G{raw['phone'][-2:]}",
        name=raw["name"],
        phone_e164=f"+91{raw['phone']}",
        address=raw.get("address", ""),
        pincode=raw.get("pincode", ""),
        district=raw.get("district") or "",
        current_bill=Decimal(str(raw["currentBillAmount"])),
        bill_cycle=cycle,
    )


def _data(case: dict, customer: Customer) -> dict:
    raw = case["input"]
    selections = {}
    if raw.get("applianceRows"):
        selections["appliance_rows"] = raw["applianceRows"]
    return {
        "customer_uid": customer.uid,
        "system_type": raw["systemType"].upper(),
        "tier": TIERS[raw["tier"]],
        "size_key": raw["size"],
        "phase": raw.get("phase"),
        "future_size_key": raw.get("futureSystemSize") or "",
        "roof_type": raw["roofType"],
        "distance_km": Decimal(str(raw["distanceKm"])),
        "vehicle_type": raw["vehicleType"],
        "subsidy_type": raw["subsidyType"],
        "ghs_houses": raw.get("ghsHouses"),
        "language": raw["quotationLanguage"],
        "selections": selections,
    }


def test_five_representative_quotations_equal_the_flarize_engines(make_user):
    user = make_user(grants={"quotations": "*", "customers": "*", "pricing_internal": ["view"]}, scopes={"quotations": "all", "customers": "all"})
    flarize.import_world()
    golden = flarize.golden()
    assert len(golden["cases"]) == 5
    for case in golden["cases"]:
        assert "error" not in case, case["id"]
        customer = _customer(case)
        with freeze_time(AT):
            quotation = quotation_services.create(user=user, data=_data(case, customer))
            version = quotation_services.issue(quotation.current_version, user=user)
        document = version.document_payload
        theirs = case["document"]
        where = case["id"]
        compare_payload(document["payload"], theirs["payload"], where, _offer(case, case["input"]["tier"]))
        mine_alternatives = {entry["tier"]: entry for entry in document["payload"].get("alternatives") or []}
        theirs_alternatives = {entry["tier"]: entry for entry in theirs["payload"].get("alternatives") or []}
        assert sorted(mine_alternatives) == sorted(theirs_alternatives), f"{where}: alternative tiers"
        for tier, entry in theirs_alternatives.items():
            assert mine_alternatives[tier]["optionIndex"] == entry["optionIndex"]
            compare_payload(mine_alternatives[tier]["payload"], entry["payload"], f"{where}/{tier}", _offer(case, tier))
        compare_freeze(document["snapshot"], theirs["snapshot"], where)
        for tier, entry in case["tiers"].items():
            bom = BomSnapshot.objects.get(quotation_version=version, tier=TIERS[tier])
            commercial = CommercialSnapshot.objects.get(quotation_version=version, tier=TIERS[tier])
            assert bom.is_primary == entry["primary"] == commercial.is_primary
            compare_lock(bom.record, entry["bomSnapshot"], f"{where}/{tier}")
            compare_snapshot(commercial.record, entry["commercialSnapshot"], f"{where}/{tier}")
        result = case["result"]
        assert version.customer_price_incl_gst == Decimal(str(result["pricing"]["customerPrice"]))
        total = Decimal(str(result["pricing"].get("customerTotal", result["pricing"]["customerPrice"])))
        assert version.final_price == total - Decimal(str(_offer(case, case["input"]["tier"]) or 0))  # D-4
        assert quotation.__class__.objects.get(pk=quotation.pk).valid_until == dt.date.fromisoformat(result["quotation"]["validUntil"][:10])


@pytest.mark.skipif(not (shutil.which("node") or Path("/opt/node22/bin/node").exists()) or not Path("/home/user/flarize-main/flarize").exists(), reason="needs node and the Flarize sources")
def test_golden_capture_is_reproducible(tmp_path):
    node = shutil.which("node") or "/opt/node22/bin/node"
    out = tmp_path / "golden.json"
    subprocess.run([node, str(Path(__file__).parent / "golden" / "generate_quotations.mjs")], check=True, env={**os.environ, "TZ": "UTC", "GOLDEN_OUT": str(out)}, capture_output=True)
    assert out.read_bytes() == flarize.GOLDEN.read_bytes()
