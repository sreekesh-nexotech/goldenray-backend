"""Golden parity: bomRoles, componentIdentity, catalogLifecycle, projectBom, bomSnapshot, bomStatus."""

from __future__ import annotations

import pytest

from engines import bom_domain
from engines.tests.rules_golden import assert_error, assert_same, cases, frozen, ids, load

FILE = "rules_bom.json"
HEADER = load(FILE)["header"]
CATALOG = frozen(load("rules_catalog.json")["sections"]["catalog"][0]["catalog"])


def test_taxonomy_constants():
    assert_same(HEADER["roles"], {role.value: role.value for role in bom_domain.Role})
    assert_same(HEADER["categoryToRole"], dict(bom_domain.CATEGORY_TO_ROLE))
    assert_same(HEADER["enphaseComponentRoles"], dict(bom_domain.ENPHASE_COMPONENT_ROLES))
    assert_same(HEADER["projectEditableRoles"], list(bom_domain.PROJECT_EDITABLE_ROLES))


@pytest.mark.parametrize("case", cases(FILE, "roles"), ids=ids(cases(FILE, "roles")))
def test_role_for_line(case):
    assert bom_domain.role_for_line(frozen(case["input"]["line"])) == case["output"]


def _catalog(data):
    return CATALOG if data.get("catalogRef") == "approved" else frozen(data.get("catalog"))


@pytest.mark.parametrize("case", cases(FILE, "identity"), ids=ids(cases(FILE, "identity")))
def test_component_identity(case):
    data, fn = case["input"], case["fn"]
    if fn == "toComponent":
        result = bom_domain.to_component(data["categoryKey"], frozen(data["item"]))
    elif fn == "findByComponentId":
        result = bom_domain.find_by_component_id(CATALOG, data["componentId"])
    elif fn == "isSelectable":
        result = bom_domain.is_selectable(frozen(data["component"]))
    elif fn == "findDuplicateIdentities":
        result = bom_domain.find_duplicate_identities(CATALOG)
    else:
        result = len(bom_domain.all_components(CATALOG))
    assert_same(case["output"], result)


@pytest.mark.parametrize("case", cases(FILE, "lifecycle"), ids=ids(cases(FILE, "lifecycle")))
def test_catalog_lifecycle(case):
    data, fn = case["input"], case["fn"]
    if fn == "classifyCatalogItem":
        from engines.jscompat import UNDEFINED

        key = data.get("categoryKey", UNDEFINED)
        result = bom_domain.classify_catalog_item(key, frozen(data["item"]) if "item" in data else UNDEFINED, frozen(data["context"]))
    elif fn == "classifyCatalog":
        result = bom_domain.classify_catalog(CATALOG)
    else:
        result = bom_domain.find_exclusion_gaps(_catalog(data), *([tuple(data["tiers"])] if "tiers" in data else []))
    assert_same(case["output"], result)


_CREATE = {
    "projectId": "project_id",
    "packageId": "package_id",
    "architecture": "architecture",
    "sysType": "sys_type",
    "tier": "tier",
    "sizeKw": "size_kw",
    "phase": "phase",
    "lines": "lines",
    "createdBy": "created_by",
    "createdAt": "created_at",
}
_EDIT = {"role": "role", "componentId": "component_id", "quantity": "quantity", "selectedBy": "selected_by", "at": "at", "reason": "reason", "method": "method"}


def _kwargs(arguments, mapping):
    return {mapping[key]: frozen(value) for key, value in (arguments or {}).items()}


@pytest.mark.parametrize("case", cases(FILE, "projectBom"), ids=ids(cases(FILE, "projectBom")))
def test_project_bom(case):
    bom = frozen(case["start"])
    for step in case["steps"]:
        op, argument = step["op"], step["arg"]

        def run():
            nonlocal bom
            if op == "create":
                return bom_domain.create_project_bom(**_kwargs(argument, _CREATE))
            if op == "setComponent":
                bom = bom_domain.set_component(bom, **_kwargs(argument, _EDIT))
                return bom
            if op == "setQuantity":
                bom = bom_domain.set_quantity(bom, **_kwargs(argument, _EDIT))
                return bom
            if op == "removeRole":
                bom = bom_domain.remove_role(bom, **_kwargs(argument, _EDIT))
                return bom
            if op == "effectiveLine":
                return bom_domain.effective_line(bom, argument["role"])
            if op == "effectiveLines":
                return bom_domain.effective_lines(bom)
            return bom_domain.diff_against_package(bom)

        if "error" in step:
            with pytest.raises(bom_domain.BomError) as caught:
                run()
            assert_error(step["error"], caught.value)
        else:
            assert_same(step["output"], run())


@pytest.mark.parametrize("case", cases(FILE, "snapshot"), ids=ids(cases(FILE, "snapshot")))
def test_bom_snapshot(case):
    data, fn = case["input"], case["fn"]
    if fn == "createProjectBomSnapshot":
        mapping = {
            "projectId": "project_id",
            "bomLines": "bom_lines",
            "selections": "selections",
            "lockedBy": "locked_by",
            "lockedAt": "locked_at",
            "dataVersion": "data_version",
            "engineeringStatus": "engineering_status",
        }
        result = bom_domain.create_project_bom_snapshot(**_kwargs(data, mapping))
    elif fn == "applyProcurementPriceChange":
        snapshot = frozen(data["snapshot"])
        result = bom_domain.apply_procurement_price_change(snapshot, frozen(data["updates"]))
        if snapshot is not None and snapshot["status"] == "LOCKED":
            assert result is snapshot
    elif fn == "priceForNewQuotation":
        result = bom_domain.price_for_new_quotation(frozen(data["lines"]), frozen(data["prices"]))
    elif fn == "diffSnapshotAgainstCurrentPrices":
        result = bom_domain.diff_snapshot_against_current_prices(frozen(data["snapshot"]), frozen(data["prices"]))
    else:
        result = bom_domain.revise_snapshot(frozen(data["snapshot"]), frozen(data["changes"]), revised_by=data["revisedBy"], revised_at=data["revisedAt"])
    assert_same(case["output"], result)


@pytest.mark.parametrize("case", cases(FILE, "status"), ids=ids(cases(FILE, "status")))
def test_bom_status(case):
    data = case["input"]
    if "from" in data:
        assert bom_domain.can_transition(data["from"], data["to"]) is case["output"]
    elif "validation" in data:
        assert bom_domain.status_from_validation(frozen(data["validation"])) == case["output"]
    else:
        status = data["status"]
        result = {
            "canGenerateQuotation": bom_domain.can_generate_quotation(status),
            "canLockProjectBom": bom_domain.can_lock_project_bom(status),
            "requiresApproval": bom_domain.requires_approval(status),
            "statusReason": bom_domain.status_reason(status),
            "quotationGate": bom_domain.quotation_gate(status),
        }
        assert_same(case["output"], result)
