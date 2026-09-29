"""Parity: PackRelease #1 built from the real Flarize data equals what the Flarize engines produce.

The Flarize catalog, prices, BOM configuration, pack configuration and registry pins go through the platform importers;
``publish_initial_releases`` publishes PriceRelease #1 and PackRelease #1 through the services. For each of the 90
packs of the approved configuration the golden file (``generate_packs.mjs``, the real JavaScript) holds the BOM
(``buildBom``), the FLAT-roof price (``pricePack``) and the engineering verdict (``runPackageChecker``):

* every released pack's price (incl./excl. GST, GST) and BOM lines (SKU, name, category, qty, unit price, amount, GST)
  equal the JavaScript's;
* the released set is exactly the packs Flarize could sell: priced COMPLETE and not BLOCKED by the checker;
* every excluded pack is reported with Flarize's reason (MARKET_RATE_NOT_SET / PBC BLOCK findings);
* the stored engineering run holds, per pack, the JavaScript verdict and findings;
* the typed mirror of the approved version holds the same BOM components and quantities for every pack.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from engineering.models import Run, SubjectType
from packs.models import ConfigLine, ConfigPack, ConfigStatus, ConfigVersion, PackRelease, ReleasePack
from packs.services import engine
from packs.services.legacy_import import publish_initial_releases
from packs.tests import flarize

pytestmark = pytest.mark.django_db


@pytest.fixture(scope="module")
def expected():
    data = flarize.golden()
    specs = engine.enumerate_packs(flarize.pack_store()["approved"]["config"])
    assert len(specs) == len(data["cases"]) == 90
    cases = {}
    for spec, case in zip(specs, data["cases"]):
        entry = case["input"]
        assert (entry["systemType"], entry["size"], entry["tier"], entry["futureSystemSize"] or "", entry["batteryQuantity"]) == (spec.system, spec.size, spec.tier, spec.future, spec.battery)
        cases[spec.key] = case
    return cases


def test_pack_release_1_from_the_real_flarize_data(expected):
    """One import + publish (the imports are slow); every check below runs on its result."""
    flarize.import_world()
    published = publish_initial_releases()
    check_pack_release_equals_the_flarize_engines(published, expected)
    check_every_excluded_pack_is_reported_with_the_flarize_reason(published, expected)
    check_the_stored_run_holds_the_javascript_verdicts(published, expected)
    check_the_typed_mirror_holds_the_same_boms(published, expected)
    check_the_draft_is_imported_and_mirrored_too(published)


def _sellable(case) -> bool:
    return "error" not in case and case["pricing"]["status"] == "COMPLETE" and case["checker"]["status"] != "BLOCKED"


def check_pack_release_equals_the_flarize_engines(published, expected):
    release = PackRelease.objects.get(number=published["pack_release"])
    assert release.number == 1 and release.price_release.number == 1
    rows = {row.key: row for row in ReleasePack.objects.filter(release=release)}
    assert set(rows) == {key for key, case in expected.items() if _sellable(case)}
    assert len(rows) == 11
    for key, row in rows.items():
        case = expected[key]
        customer = case["pricing"]["customer"]
        assert row.customer_price_incl_gst == Decimal(str(customer["sellingPriceIncludingGST"])), key
        assert row.customer_price_excl_gst == Decimal(str(customer["sellingPriceBeforeGST"])), key
        assert row.gst_amount == Decimal(str(customer["gstAmount"])), key
        assert row.market_rate_key == case["pricing"]["marketRateKey"]
        mine = [
            (line["sku"], line["name"], line["category"], line["qty"], line["unit_price"], line["amount"], line["gst_pct"], line["gst_amount"]) for line in row.bom if line["source"] != "STRUCTURE"
        ]
        theirs = [(line["componentId"], line["name"], line["category"], line["qty"], line["unitPrice"], line["amount"], line["gst"], line["gstAmt"]) for line in case["bom"]["lines"]]
        assert mine == theirs, key
        structure = [(line["name"], line["qty"], line["unit_price"], line["amount"]) for line in row.bom if line["source"] == "STRUCTURE"]
        assert structure == [(line["name"], line["qty"], line["unitPrice"], line["amount"]) for line in case["pricing"]["structureMaterial"]["lines"]]
        assert row.pricing["customer"] == customer
        assert row.pricing["internal"] == case["pricing"]["internal"]


def check_every_excluded_pack_is_reported_with_the_flarize_reason(published, expected):
    report = published["report"]
    matrix = {row["key"]: row for row in report["matrix"]}
    assert len(matrix) == 90
    for key, case in expected.items():
        row = matrix[key]
        if _sellable(case):
            assert row["status"] == "READY" and row["price"] == case["pricing"]["customer"]["sellingPriceIncludingGST"]
            continue
        assert row["status"] == "EXCLUDED"
        if case["pricing"]["status"] == "BLOCKED":
            assert "MARKET_RATE_NOT_SET" in row["reasons"], key
        if case["checker"]["status"] == "BLOCKED":
            assert "PACK_ENGINEERING_BLOCKED" in row["reasons"], key
    engineering = [item for item in report["items"] if item["code"] == "PACK_ENGINEERING_BLOCKED"]
    assert {item["context"]["pack"] for item in engineering} == {key for key, case in expected.items() if case["checker"]["status"] == "BLOCKED"}
    for item in engineering:
        blocked = [(f["ruleId"], f["componentIds"]) for f in expected[item["context"]["pack"]]["checker"]["findings"] if f["severity"] == "BLOCKED"]
        assert [(f["rule_code"], f["component_ids"]) for f in item["context"]["findings"]] == blocked
    assert report["counts"]["BLOCK"] == 0


def check_the_stored_run_holds_the_javascript_verdicts(published, expected):
    version = ConfigVersion.objects.get(number=16)
    run = Run.objects.filter(subject_type=SubjectType.PACK_CONFIG_VERSION, subject_uid=version.uid).latest("created_at")
    assert run.rule_set.rules_version == "phase1e.1"
    for key, case in expected.items():
        assert run.summary["scopes"][key]["status"] == case["checker"]["status"]
        assert run.summary["scopes"][key]["deterministicKey"] == case["checker"]["deterministicKey"]
    findings: dict[str, list] = {}
    for finding in run.findings.order_by("sort_order"):
        findings.setdefault(finding.context["pack"], []).append((finding.rule_code, finding.severity, finding.context["componentIds"]))
    severity = {"BLOCKED": "BLOCK", "WARNING": "WARN", "INFO": "INFO"}
    assert findings == {key: [(f["ruleId"], severity[f["severity"]], f["componentIds"]) for f in case["checker"]["findings"]] for key, case in expected.items() if case["checker"]["findings"]}
    assert run.result == "FAIL"


def check_the_typed_mirror_holds_the_same_boms(published, expected):
    version = ConfigVersion.objects.get(number=16)
    assert version.status == ConfigStatus.PUBLISHED
    packs = {pack.key: pack for pack in ConfigPack.objects.filter(config_version=version)}
    assert set(packs) == set(expected)
    lines = {}
    for line in ConfigLine.objects.filter(pack__config_version=version).exclude(source="STRUCTURE").select_related("pack", "component").order_by("pack_id", "sort_order"):
        lines.setdefault(line.pack.key, []).append((line.component.sku if line.component else None, line.name, line.qty))
    for key, case in expected.items():
        assert lines[key] == [(ln["componentId"] if ln["category"] != "fixed" else None, ln["name"], Decimal(str(ln["qty"])).quantize(Decimal("0.001"))) for ln in case["bom"]["lines"]], key
        panel = next(ln["componentId"] for ln in case["bom"]["lines"] if ln["category"] == "panel")
        assert packs[key].panel.sku == panel


def check_the_draft_is_imported_and_mirrored_too(published):
    draft = ConfigVersion.objects.get(number=17)
    assert draft.status == ConfigStatus.DRAFT and draft.based_on.number == 16
    assert draft.config == flarize.pack_store()["draft"]["config"]
    assert ConfigPack.objects.filter(config_version=draft).count() == 90
    history = ConfigVersion.objects.filter(status=ConfigStatus.SUPERSEDED).order_by("number")
    assert [v.number for v in history] == list(range(1, 16)) and all(v.config is None for v in history)


NODE = os.environ.get("NODE_BIN") or shutil.which("node") or ("/opt/node22/bin/node" if Path("/opt/node22/bin/node").exists() else None)
FLARIZE_ROOT = Path(os.environ.get("FLARIZE_ROOT", "/home/user/flarize-main/flarize"))


@pytest.mark.skipif(NODE is None or not FLARIZE_ROOT.joinpath("server-pack-publish.js").exists(), reason="needs node and the Flarize sources (FLARIZE_ROOT)")
def test_golden_capture_is_reproducible(tmp_path):
    """Re-running the generator against the real JavaScript yields the committed golden file byte for byte."""
    out = tmp_path / "flarize_packs.json"
    environment = {**os.environ, "GOLDEN_OUT": str(out), "TZ": "UTC"}
    subprocess.run([NODE, "--no-warnings", str(flarize.GOLDEN.parent / "generate_packs.mjs"), str(FLARIZE_ROOT)], check=True, env=environment, capture_output=True, timeout=300)
    assert out.read_bytes() == flarize.GOLDEN.read_bytes()
