"""The closed registry must match PLAN §3.2 exactly; normalisation removes anything it does not contain."""

import pytest

from accounts import registry
from accounts.registry import MODULES, RegistryError, as_dict, full_access, grants_from_spec, is_allowed, normalise_permissions, normalise_scopes

# PLAN §3.2, transcribed independently of the implementation.
PLAN_TABLE = {
    "dashboard": ("GENERAL", ["view"]),
    "catalog": ("PRODUCT", ["view", "create", "edit", "approve", "archive"]),
    "pricing": ("PRODUCT", ["view", "edit", "publish"]),
    "pricing_internal": ("PRODUCT", ["view"]),
    "market_rates": ("PRODUCT", ["view", "edit", "publish"]),
    "offers": ("PRODUCT", ["view", "create", "edit", "approve", "publish", "archive"]),
    "procurement": ("PRODUCT", ["view", "create", "edit", "commit"]),
    "inventory": ("PRODUCT", ["view", "edit"]),
    "bom": ("CONFIG", ["view", "edit"]),
    "packs": ("CONFIG", ["view", "edit", "submit", "approve", "publish"]),
    "engineering": ("CONFIG", ["view", "verify", "approve"]),
    "leads": ("SALES", ["view", "create", "edit", "archive", "manage"]),
    "customers": ("SALES", ["view", "create", "edit", "archive", "manage"]),
    "quotations": ("SALES", ["view", "create", "edit", "issue", "revise", "approve", "archive"]),
    "quotation_content": ("SALES", ["view", "edit", "publish"]),
    "agreements": ("SALES", ["view", "create", "edit", "issue", "manage"]),
    "site_inspections": ("SALES", ["view", "create", "edit", "assign", "submit", "approve", "release", "archive"]),
    "projects": ("SALES", ["view", "create", "edit", "lock", "archive"]),
    "pages": ("WEBSITE", ["view", "edit", "publish", "verify"]),
    "blogs": ("WEBSITE", ["view", "create", "edit", "publish", "verify", "archive"]),
    "faqs": ("WEBSITE", ["view", "create", "edit", "publish", "verify", "archive"]),
    "media": ("WEBSITE", ["view", "create", "edit", "archive"]),
    "seo": ("WEBSITE", ["view", "edit", "publish"]),
    "products_public": ("WEBSITE", ["view", "edit", "publish"]),
    "reference_data": ("WEBSITE", ["view", "create", "edit", "archive"]),
    "emi": ("WEBSITE", ["view", "edit"]),
    "job_positions": ("CAREERS", ["view", "create", "edit", "publish", "verify", "archive"]),
    "applications": ("CAREERS", ["view", "edit", "archive"]),
    "departments": ("CAREERS", ["view", "create", "edit", "archive"]),
    "career_page": ("CAREERS", ["view", "edit", "publish"]),
    "employees": ("HR", ["view", "create", "edit", "archive"]),
    "hr_setup": ("HR", ["view", "edit"]),
    "attendance": ("HR", ["view", "edit", "export", "manage"]),
    "leave": ("HR", ["view", "create", "approve", "archive"]),
    "devices": ("HR", ["view", "create", "edit", "sync", "manage"]),
    "company": ("ADMIN", ["view", "edit"]),
    "users": ("ADMIN", ["view", "create", "edit", "archive", "manage"]),
    "roles": ("ADMIN", ["view", "create", "edit", "manage"]),
    "settings": ("ADMIN", ["view", "edit"]),
    "audit": ("ADMIN", ["view"]),
}
PLAN_SCOPES = {
    **{module: ("all", "owned") for module in ("customers", "quotations", "agreements", "leads")},
    "site_inspections": ("all", "owned", "assigned"),
    **{module: ("all", "office", "self") for module in ("employees", "attendance", "leave")},
}
PLAN_VERBS = "view create edit publish verify archive manage approve submit lock commit issue revise assign release export sync".split()


def test_registry_matches_the_plan_exactly():
    assert list(MODULES) == list(PLAN_TABLE)
    for module, (group, actions) in PLAN_TABLE.items():
        spec = MODULES[module]
        assert spec.group == group, module
        assert sorted(spec.actions) == sorted(actions), module


def test_scopes_match_the_plan():
    for module, spec in MODULES.items():
        assert spec.scopes == PLAN_SCOPES.get(module, ("all",)), module
        assert spec.default_scope in spec.scopes
        if len(spec.scopes) > 1:
            assert spec.default_scope != "all", f"{module}: a missing scope must default to a narrow one"


def test_verbs_match_the_plan():
    assert list(registry.ACTIONS) == PLAN_VERBS
    assert set(registry.ACTION_LABELS) == set(PLAN_VERBS)
    used = {action for spec in MODULES.values() for action in spec.actions}
    assert used <= set(PLAN_VERBS)


def test_self_action_guard_data():
    assert registry.SELF_ACTION_DENIED == {("attendance", "edit"), ("leave", "approve")}
    assert all(is_allowed(module, action) for module, action in registry.SELF_ACTION_DENIED)


def test_is_allowed():
    assert is_allowed("catalog", "approve")
    assert not is_allowed("catalog", "publish")
    assert not is_allowed("nope", "view")


class TestNormalisePermissions:
    def test_orders_deduplicates_and_drops_unknown(self):
        result = normalise_permissions({"users": ["manage", "view", "view"], "catalog": ["teleport", "view"], "nope": ["view"], "audit": []})
        assert result == {"catalog": ["view"], "users": ["view", "manage"]}
        assert list(result) == ["catalog", "users"]

    def test_accepts_a_single_action_string_and_sets(self):
        assert normalise_permissions({"audit": "view", "bom": {"edit", "view"}}) == {"bom": ["view", "edit"], "audit": ["view"]}

    @pytest.mark.parametrize("value", [None, [], "catalog", 5])
    def test_non_mapping_input_is_empty(self, value):
        assert normalise_permissions(value) == {}

    def test_strict_reports_every_problem(self):
        with pytest.raises(RegistryError) as excinfo:
            normalise_permissions({"nope": ["view"], "catalog": ["view", "teleport"], "bom": 3}, strict=True)
        assert excinfo.value.errors == {"nope": ["Unknown module."], "catalog": ["Unknown action 'teleport'."], "bom": ["Actions must be a list."]}
        with pytest.raises(RegistryError):
            normalise_permissions(["catalog"], strict=True)

    def test_strict_accepts_valid_input(self):
        assert normalise_permissions({"catalog": ["view"]}, strict=True) == {"catalog": ["view"]}


class TestNormaliseScopes:
    def test_missing_scope_defaults_to_the_narrowest(self):
        permissions = {"customers": ["view"], "attendance": ["view"], "catalog": ["view"], "site_inspections": ["view"]}
        assert normalise_scopes({}, permissions) == {"catalog": "all", "customers": "owned", "site_inspections": "owned", "attendance": "self"}

    def test_valid_scopes_are_kept_and_others_dropped(self):
        permissions = {"customers": ["view"], "employees": ["view"]}
        scopes = {"customers": "all", "employees": "office", "leads": "all", "catalog": "all"}
        assert normalise_scopes(scopes, permissions) == {"customers": "all", "employees": "office"}

    def test_invalid_scope_falls_back_leniently_and_fails_strictly(self):
        assert normalise_scopes({"catalog": "owned"}, {"catalog": ["view"]}) == {"catalog": "all"}
        with pytest.raises(RegistryError) as excinfo:
            normalise_scopes({"catalog": "owned", "nope": "all"}, {"catalog": ["view"]}, strict=True)
        assert set(excinfo.value.errors) == {"catalog", "nope"}
        with pytest.raises(RegistryError):
            normalise_scopes(["all"], strict=True)

    def test_without_permissions_uses_the_scope_keys(self):
        assert normalise_scopes({"leads": "all", "nope": "all"}) == {"leads": "all"}
        assert normalise_scopes(None) == {}
        assert normalise_scopes("bad") == {}


def test_full_access():
    permissions, scopes = full_access()
    assert permissions == {module: list(spec.actions) for module, spec in MODULES.items()}
    assert set(scopes.values()) == {"all"} and set(scopes) == set(MODULES)
    assert normalise_permissions(permissions, strict=True) == permissions


def test_grants_from_spec():
    assert grants_from_spec({"blogs": "*", "media": ["view", "create"]}) == {"blogs": list(MODULES["blogs"].actions), "media": ["view", "create"]}
    with pytest.raises(RegistryError):
        grants_from_spec({"nope": "*"})
    with pytest.raises(RegistryError):
        grants_from_spec({"media": ["teleport"]})


def test_as_dict_describes_every_module_once():
    data = as_dict()
    modules = [module["key"] for group in data["groups"] for module in group["modules"]]
    assert modules == list(MODULES)
    assert [group["key"] for group in data["groups"]] == list(registry.GROUPS)
    assert {scope["key"] for scope in data["scopes"]} == set(registry.SCOPES)
    pricing_internal = next(module for group in data["groups"] for module in group["modules"] if module["key"] == "pricing_internal")
    assert pricing_internal["permission_only"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"key": "x", "group": "PRODUCT", "label": "X", "actions": ("teleport",)},
        {"key": "x", "group": "NOPE", "label": "X", "actions": ("view",)},
        {"key": "x", "group": "PRODUCT", "label": "X", "actions": ("view",), "scopes": ("all",), "default_scope": "owned"},
    ],
)
def test_module_spec_validation(kwargs):
    with pytest.raises(ValueError):
        registry.ModuleSpec(**kwargs)
