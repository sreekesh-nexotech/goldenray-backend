"""Golden parity: quotation payload, frozen-document replay, commercial snapshots, commercial freeze, validity policy.

``replay`` cases rebuild issued documents of ``data/quotation-state.json`` (personal data masked): the generator proved
that the real JavaScript reproduces each frozen document (``frozenPayloadVerdict``, ``matches``); here the Python
engines must reproduce the JavaScript exactly — payload (primary + every alternative tier), the pack commercial
snapshots, the commercial freeze, the renderer pin and the document itself.
"""

from __future__ import annotations

import pytest

from engines import gate, quotation_payload
from engines.frozen import is_frozen, sha256_hex
from engines.tests.rules_golden import assert_error, assert_same, cases, frozen, ids, load

FILE = "rules_payload.json"
GOLDEN = load(FILE)
CATALOG = frozen(load("rules_catalog.json")["sections"]["catalog"][0]["catalog"])

_PACK = {
    "snapshotId": "snapshot_id",
    "projectId": "project_id",
    "bomSnapshotId": "bom_snapshot_id",
    "packPricing": "pack_pricing",
    "configVersion": "config_version",
    "materialList": "material_list",
    "costResult": "cost_result",
    "issuedBy": "issued_by",
    "issuedAt": "issued_at",
    "moneyRuleVersion": "money_rule_version",
    "offer": "offer",
    "catalogVersion": "catalog_version",
}
_GROSS = {key: _PACK[key] for key in ("snapshotId", "costResult", "issuedBy", "issuedAt", "moneyRuleVersion", "offer")} | {"pricingResult": "pricing_result"}
_FREEZE = {
    "bom": "bom",
    "project": "project",
    "transportationConfig": "transportation_config",
    "commercialSnapshot": "commercial_snapshot",
    "subsidyResult": "subsidy_result",
    "brandingStore": "branding_store",
    "brandingOverrides": "branding_overrides",
    "policy": "policy",
    "validityOverrideDays": "validity_override_days",
    "issuedAt": "issued_at",
    "rendererPin": "renderer_pin",
    "packageProfile": "package_profile",
    "productionMode": "production_mode",
}


def _kwargs(arguments: dict, mapping: dict) -> dict:
    return {name: frozen(arguments[key]) for key, name in mapping.items() if key in arguments}


def test_version_keys_and_payload_version():
    header = GOLDEN["header"]
    assert header["payloadVersion"] == quotation_payload.QUOTATION_PAYLOAD_VERSION
    assert header["versionKeys"] == list(quotation_payload.VERSION_KEYS)
    assert len(quotation_payload.VERSION_KEYS) == 13


def test_every_stored_document_replays_in_javascript():
    """The replay evidence the generator recorded for all 64 issued documents."""
    summary = GOLDEN["header"]["replaySummary"]
    assert len(summary) == GOLDEN["header"]["replayedDocuments"] == 64
    verdicts = [entry["payload"] for entry in summary.values()]
    assert sum(verdict == "identical" for verdict in verdicts) >= 2
    assert all(verdict.startswith("identical") or verdict.startswith("differs at $.testimonials.reason") for verdict in verdicts), verdicts
    assert all(entry["freeze"] in ("identical", "no freeze") for entry in summary.values())
    assert all(entry["rendererPin"] in ("identical", "none") for entry in summary.values())
    snapshots = [part for entry in summary.values() for part in entry["commercialSnapshots"].split("; ")]
    assert all(part in ("identical", "not a pack snapshot") or part.startswith("identical apart from members added after issue") for part in snapshots)


@pytest.mark.parametrize("case", cases(FILE, "replay"), ids=ids(cases(FILE, "replay")))
class TestReplay:
    def test_payload_with_alternatives(self, case):
        primary = quotation_payload.build_quotation_payload_from(frozen(case["primaryInputs"]))
        entries = []
        for index, alternative in enumerate(case["alternatives"]):
            inputs = frozen(alternative["inputs"])
            payload = quotation_payload.build_quotation_payload_from(inputs)
            entries.append(quotation_payload.alternative_entry(index, frozen(alternative["option"]), inputs["system"], payload))
        complete = quotation_payload.with_alternatives(primary, entries)
        assert case["frozenPayloadVerdict"]["verdict"] != "differs"
        assert_same(case["expectedPayload"], complete)
        assert is_frozen(complete)
        assert len(sha256_hex(complete)) == 64 and sha256_hex(complete) == sha256_hex(frozen(case["expectedPayload"]))

    def test_record_inputs(self, case):
        record, primary = frozen(case["record"]), case["primaryInputs"]
        inputs = gate.resolve_inputs(record, primary["quotation"]["quotationVersion"], bom_snapshot=frozen(primary["bomSnapshot"]), commercial_snapshot=frozen(primary["commercialSnapshot"]))
        for key in ("customer", "site", "system", "bomSnapshot", "commercialSnapshot", "engineering", "upgradeIdentity", "variant"):
            assert_same(primary[key], inputs[key])
        assert_same({**primary["quotation"], "status": record["status"]}, inputs["quotation"])
        for index, alternative in enumerate(case["alternatives"]):
            option, expected = frozen(alternative["option"]), alternative["inputs"]
            rebuilt = quotation_payload.alternative_inputs(
                record, option, expected["quotation"]["quotationVersion"], bom_snapshot=frozen(expected["bomSnapshot"]), commercial_snapshot=frozen(expected["commercialSnapshot"])
            )
            for key in ("quotation", "customer", "site", "system", "bomSnapshot", "commercialSnapshot", "engineering", "upgradeIdentity", "variant"):
                assert_same(expected[key], rebuilt[key])

    def test_commercial_snapshots(self, case):
        assert case["commercial"], "every replayed document carries pack snapshots"
        for snapshot in case["commercial"]:
            assert snapshot["matches"] is True
            assert_same(snapshot["output"], quotation_payload.create_pack_commercial_snapshot(**_kwargs(snapshot["args"], _PACK)))

    def test_commercial_freeze_and_document(self, case):
        freeze = case["freeze"]
        assert freeze["matches"] is True and case["frozenSnapshotMatchesFreeze"] is True
        snapshot = quotation_payload.build_commercial_snapshot(**_kwargs(freeze["args"], _FREEZE))
        assert_same(freeze["output"], snapshot)
        document = case["document"]
        pin = quotation_payload.normalize_renderer_pinning(frozen(document["rendererPinning"]), document["issuedAt"])
        assert_same(document["rendererPinning"], pin)
        issued = quotation_payload.issued_document(
            quotation_id=document["quotationId"],
            version=document["version"],
            issued_by=document["issuedBy"],
            issued_by_role=document["issuedByRole"],
            issued_at=document["issuedAt"],
            commercial_snapshot_id=document["commercialSnapshotId"],
            bom_snapshot_id=document["bomSnapshotId"],
            cms_page_versions=document["cmsPageVersions"],
            renderer_pinning=pin,
            payload=frozen(case["expectedPayload"]),
            snapshot=snapshot,
        )
        assert_same({**document, "payload": case["expectedPayload"], "snapshot": freeze["output"]}, issued)
        stored = frozen(case["primaryInputs"]["commercialSnapshot"])
        verdict = quotation_payload.verify_immutability(issued, stored)
        assert verdict["frozen"] and verdict["snapshotPresent"] and verdict["matchesSnapshot"]
        assert not quotation_payload.verify_immutability(issued, None)["matchesSnapshot"]


@pytest.mark.parametrize("case", cases(FILE, "payload"), ids=ids(cases(FILE, "payload")))
def test_payload(case):
    assert_same(case["output"], quotation_payload.build_quotation_payload_from(frozen(case["input"])))


@pytest.mark.parametrize("case", cases(FILE, "projection"), ids=ids(cases(FILE, "projection")))
def test_projection(case):
    data, fn = case["input"], case["fn"]
    if fn == "projectIssuedPayloadForActor":
        result = quotation_payload.project_issued_payload_for_actor(frozen(data["payload"]), data["canSeeInternalCost"])
    elif fn == "projectIssuedSnapshotForActor":
        result = quotation_payload.project_issued_snapshot_for_actor(frozen(data["snapshot"]), data["canSeeInternalCost"])
    elif fn == "normalizeRendererPinning":
        if "error" in case:
            with pytest.raises(quotation_payload.RendererPinningInvalid) as caught:
                quotation_payload.normalize_renderer_pinning(frozen(data["pinning"]), data["at"])
            assert_error(case["error"], caught.value)
            return
        result = quotation_payload.normalize_renderer_pinning(frozen(data["pinning"]), data["at"])
    elif fn == "componentAttributesFor":
        result = quotation_payload.component_attributes_for(CATALOG if data.get("catalogRef") == "approved" else frozen(data["catalog"]), data["componentIds"])
    else:
        result = quotation_payload.resolve_panel_dcr_type(frozen(data["bomSnapshot"]), frozen(data["componentAttributes"]))
    assert_same(case["output"], result)


@pytest.mark.parametrize("case", cases(FILE, "commercialSnapshot"), ids=ids(cases(FILE, "commercialSnapshot")))
def test_commercial_snapshot(case):
    data, fn = case["input"], case["fn"]
    if fn == "createCommercialSnapshot":
        call = lambda: quotation_payload.create_commercial_snapshot(**_kwargs(data, _GROSS))  # noqa: E731
    elif fn == "createPackCommercialSnapshot":
        call = lambda: quotation_payload.create_pack_commercial_snapshot(**_kwargs(data, _PACK))  # noqa: E731
    elif fn == "applyConfigChange":
        snapshot = frozen(data["snapshot"])
        result = quotation_payload.apply_config_change(snapshot, frozen(data["change"]) if "change" in data else None)
        assert result["snapshot"] is snapshot and result["mutated"] is False
        call = lambda: result  # noqa: E731
    elif fn == "supersede":
        call = lambda: quotation_payload.supersede(frozen(data["snapshot"]), superseded_by=data["supersededBy"], at=data["at"])  # noqa: E731
    else:
        call = lambda: quotation_payload.compare_recalculation(frozen(data["previousSnapshot"]), frozen(data["cost"]), frozen(data["pricing"]))  # noqa: E731
    if "error" in case:
        with pytest.raises(quotation_payload.SnapshotError) as caught:
            call()
        assert_error(case["error"], caught.value)
    else:
        assert_same(case["output"], call())


#: Pinned divergences (DV-24 principle): the JavaScript printed a binary64 artefact, Python publishes the exact value.
FREEZE_DIVERGENCES = {"gross-margin snapshot, 0.07 target margin": {"$.commercial.targetMarginPct": "representation"}}


@pytest.mark.parametrize("case", cases(FILE, "freeze"), ids=ids(cases(FILE, "freeze")))
def test_freeze(case):
    call = lambda: quotation_payload.build_commercial_snapshot(**_kwargs(case["input"], _FREEZE))  # noqa: E731
    if "error" in case:
        with pytest.raises((quotation_payload.FreezeError, quotation_payload.PolicyError)) as caught:
            call()
        assert_error(case["error"], caught.value)
    else:
        assert_same(case["output"], call(), allowed=FREEZE_DIVERGENCES.get(case["id"]))


def test_the_pinned_margin_divergence_is_the_exact_percentage():
    case = next(item for item in cases(FILE, "freeze") if item["id"] in FREEZE_DIVERGENCES)
    python = quotation_payload.build_commercial_snapshot(**_kwargs(case["input"], _FREEZE))["commercial"]["targetMarginPct"]
    assert str(python) == "7.00"  # 0.07 × 100, exactly
    assert str(case["output"]["commercial"]["targetMarginPct"]) == "7.000000000000001"  # binary64 0.07 × 100


@pytest.mark.parametrize("case", cases(FILE, "policy"), ids=ids(cases(FILE, "policy")))
def test_policy(case):
    data, fn = case["input"], case["fn"]
    store = frozen(data.get("store"))
    if fn == "resolveEffectiveValidity":
        call = lambda: quotation_payload.resolve_effective_validity(store, data["overrideDays"], data["issuedAt"])  # noqa: E731
    elif fn == "resolveActivePolicy":
        call = lambda: quotation_payload.resolve_active_policy(store, data["issuedAt"])  # noqa: E731
    elif fn == "validatePolicyRecord":
        call = lambda: (quotation_payload.validate_policy_record(frozen(data["policy"])), "ok")[1]  # noqa: E731
    elif fn == "publishPolicy":
        call = lambda: quotation_payload.publish_policy(store, actor_id=data["actorId"], policy=frozen(data["policy"]), at=data["at"])  # noqa: E731
    elif fn == "setPolicyStatus":
        call = lambda: quotation_payload.set_policy_status(store, actor_id=data["actorId"], policy_id=data["policyId"], status=data["status"], at=data["at"], reason=data.get("reason"))  # noqa: E731
    elif fn == "setDefaultValidityDays":
        call = lambda: quotation_payload.set_default_validity_days(store, actor_id=data["actorId"], default_days=data["defaultDays"], at=data["at"])  # noqa: E731
    else:
        call = lambda: quotation_payload.describe_policy_store(store, data["now"])  # noqa: E731
    if "error" in case:
        with pytest.raises(quotation_payload.PolicyError) as caught:
            call()
        assert_error(case["error"], caught.value)
    else:
        assert_same(case["output"], call())
