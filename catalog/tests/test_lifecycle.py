"""Component status lifecycle (activate / deprecate / retire), assert_selectable, usage registry and delete guard."""

import pytest

from audit.models import AuditLog
from catalog.models import ComponentChange, ComponentStatus
from catalog.services import lifecycle, usage
from catalog.tests.factories import CategoryFactory, ComponentFactory, inverter, panel, panel_category
from core.models import OutboxEvent

pytestmark = pytest.mark.django_db
URL = "/api/v1/catalog/components/"


def action(component, name):
    return f"{URL}{component.uid}/{name}/"


class TestActivate:
    def test_draft_to_active(self, client, catalog_user):
        component = panel(status=ComponentStatus.DRAFT)
        response = client.post(action(component, "activate"), {"expected_version": 1, "reason": "approved"}, format="json")
        assert response.status_code == 200 and response.json()["status"] == "ACTIVE" and response.json()["version"] == 2
        change = ComponentChange.objects.get(component=component, field="status")
        assert (change.old, change.new, change.reason) == ("DRAFT", "ACTIVE", "approved")
        assert AuditLog.objects.get(action="catalog.component_activated").actor == catalog_user
        event = OutboxEvent.objects.get(event_type="catalog.component_status_changed")
        assert event.payload["from"] == "DRAFT" and event.payload["to"] == "ACTIVE"

    def test_spec_required_for_panels_and_inverters(self, client):
        component = ComponentFactory(category=panel_category(), status=ComponentStatus.DRAFT)
        response = client.post(action(component, "activate"), {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "spec_required"
        accessory = ComponentFactory(status=ComponentStatus.DRAFT)
        assert client.post(action(accessory, "activate"), {}, format="json").json()["status"] == "ACTIVE"

    def test_inactive_category_and_invalid_transition(self, client):
        component = ComponentFactory(status=ComponentStatus.DRAFT, category=CategoryFactory(is_active=False))
        assert client.post(action(component, "activate"), {}, format="json").json()["code"] == "category_inactive"
        active = ComponentFactory()
        response = client.post(action(active, "activate"), {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "invalid_status_transition"

    def test_reactivating_a_deprecated_component_clears_the_replacement(self, client):
        replacement = panel()
        component = panel(status=ComponentStatus.DEPRECATED, deprecated_reason="old", replacement=replacement)
        body = client.post(action(component, "activate"), {}, format="json").json()
        assert body["status"] == "ACTIVE" and body["replacement"] is None and body["deprecated_reason"] == ""

    def test_stale_version(self, client):
        component = panel(status=ComponentStatus.DRAFT, version=3)
        assert client.post(action(component, "activate"), {"expected_version": 2}, format="json").json()["code"] == "stale_version"


class TestDeprecate:
    def test_with_replacement(self, client):
        component, replacement = panel(sku="p1"), panel(sku="p2")
        response = client.post(action(component, "deprecate"), {"reason": "Superseded by 550 W", "replacement_uid": str(replacement.uid)}, format="json")
        body = response.json()
        assert response.status_code == 200 and body["status"] == "DEPRECATED" and body["replacement"]["sku"] == "p2" and body["deprecated_reason"] == "Superseded by 550 W"
        assert lifecycle.selection_warning(type(component).objects.get(pk=component.pk)) == "p1 is deprecated (Superseded by 550 W). Suggested replacement: p2."
        assert OutboxEvent.objects.get(event_type="catalog.component_status_changed").payload["replacement_uid"] == str(replacement.uid)

    def test_reason_required(self, client):
        component = panel()
        assert client.post(action(component, "deprecate"), {}, format="json").json()["errors"]["reason"]
        assert client.post(action(component, "deprecate"), {"reason": "   "}, format="json").json()["errors"]["reason"]

    @pytest.mark.parametrize("case", ["self", "retired", "other_category", "cycle"])
    def test_replacement_rules(self, client, case):
        component = panel(sku="p1")
        if case == "self":
            replacement = component
        elif case == "retired":
            replacement = panel(sku="p2", status=ComponentStatus.RETIRED)
        elif case == "other_category":
            replacement = inverter()
        else:
            replacement = panel(sku="p2", status=ComponentStatus.DEPRECATED, replacement=component, deprecated_reason="x")
        response = client.post(action(component, "deprecate"), {"reason": "x", "replacement_uid": str(replacement.uid)}, format="json")
        assert response.status_code == 400 and response.json()["errors"]["replacement_uid"]

    def test_only_active_components(self, client):
        component = panel(status=ComponentStatus.DRAFT)
        assert client.post(action(component, "deprecate"), {"reason": "x"}, format="json").json()["code"] == "invalid_status_transition"


class TestRetire:
    @pytest.mark.parametrize("status", ["DRAFT", "ACTIVE", "DEPRECATED"])
    def test_retire_is_terminal(self, client, status):
        component = panel(status=status)
        body = client.post(action(component, "retire"), {"reason": "End of life"}, format="json").json()
        assert body["status"] == "RETIRED" and body["retired_reason"] == "End of life"
        for name in ("activate", "deprecate", "retire"):
            assert client.post(action(component, name), {"reason": "x"}, format="json").json()["code"] == "invalid_status_transition"


class TestAssertSelectable:
    def test_rules(self):
        replacement = panel(sku="p2")
        retired = panel(sku="p1", status=ComponentStatus.RETIRED, replacement=replacement)
        with pytest.raises(lifecycle.ComponentNotSelectable) as retired_error:
            lifecycle.assert_selectable(retired)
        assert retired_error.value.code == "component_retired" and "Use p2 instead" in retired_error.value.message
        deleted = panel(sku="p3")
        deleted.soft_delete()
        with pytest.raises(lifecycle.ComponentNotSelectable) as deleted_error:
            lifecycle.assert_selectable(deleted, field="panel")
        assert deleted_error.value.code == "component_deleted" and "panel" in deleted_error.value.errors
        with pytest.raises(lifecycle.ComponentNotSelectable) as missing:
            lifecycle.assert_selectable(None)
        assert missing.value.code == "component_not_found"
        for status in ("DRAFT", "ACTIVE", "DEPRECATED"):
            component = panel(status=status)
            assert lifecycle.assert_selectable(component) is component
        assert lifecycle.selection_warning(replacement) is None
        # A replacement retired since must not be suggested (it is not selectable either).
        type(replacement).objects.filter(pk=replacement.pk).update(status=ComponentStatus.RETIRED)
        retired.refresh_from_db()
        with pytest.raises(lifecycle.ComponentNotSelectable) as stale_hint:
            lifecycle.assert_selectable(retired)
        assert "Use p2" not in stale_hint.value.message
        deprecated = panel(sku="p5", status=ComponentStatus.DEPRECATED, deprecated_reason="old", replacement=type(replacement).objects.get(pk=replacement.pk))
        assert lifecycle.selection_warning(deprecated) == "p5 is deprecated (old)."
        from catalog.services import assert_selectable  # the public entry point later packages call

        assert assert_selectable is lifecycle.assert_selectable


class TestUsage:
    def test_usage_endpoint_and_delete_guard(self, client):
        component = panel(sku="p1")
        dependant = panel(sku="p2", status=ComponentStatus.DEPRECATED, replacement=component, deprecated_reason="x")
        body = client.get(action(component, "usage")).json()
        assert body["in_use"] is True and body["total"] == 1 and body["component"]["sku"] == "p1"
        section = next(s for s in body["sections"] if s["name"] == "catalog.replacements")
        assert section["references"][0] == {"object_type": "catalog.component", "object_uid": str(dependant.uid), "label": f"p2 — {dependant.name}", "status": "DEPRECATED"}
        response = client.delete(f"{URL}{component.uid}/")
        assert response.status_code == 409 and response.json()["code"] == "component_in_use"
        dependant.soft_delete()
        assert client.get(action(component, "usage")).json()["in_use"] is False
        assert client.delete(f"{URL}{component.uid}/").status_code == 204

    def test_registered_providers_and_fail_closed(self, client):
        component = panel()

        @usage.register("packs.test_lines")
        def lines(target):
            return [{"object_type": "packs.configline", "object_uid": "7f1f0000-0000-4000-8000-000000000000", "label": "3 kW"}] if target.pk == component.pk else []

        @usage.register("quotations.broken")
        def broken(target):
            raise RuntimeError("down")

        try:
            body = client.get(action(component, "usage")).json()
            sections = {section["name"]: section for section in body["sections"]}
            assert sections["packs.test_lines"]["count"] == 1 and sections["quotations.broken"]["error"] is True and body["in_use"] is True
            response = client.delete(f"{URL}{component.uid}/")
            assert response.status_code == 409 and set(response.json()["errors"]) == {"packs.test_lines", "quotations.broken"}
        finally:
            usage.unregister("packs.test_lines")
            usage.unregister("quotations.broken")
        with pytest.raises(ValueError):
            usage.register("nodot")
