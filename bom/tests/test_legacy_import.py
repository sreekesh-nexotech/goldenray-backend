"""bom legacy importers: every legacy column has a home, idempotent re-runs, dry runs, D-2 in both orders."""

import copy

import pytest

from audit.models import AuditLog
from bom.models import FixedItem, PackageProfile, Slot, StructureTemplate, StructureTemplateItem, Template, TubeWeight
from bom.services import legacy_import
from bom.tests.legacy_fixtures import flarize_catalog_bom, goldenray_rows, import_legacy_database
from catalog.tests import legacy_fixtures as catalog_fixtures
from core.models import LegacyMap

pytestmark = pytest.mark.django_db


def _live_state() -> dict:
    """Everything the importers write, keyed by natural keys (for order comparisons)."""
    templates = {t.system_type: (t.name, t.description, t.sizes, t.three_phase_sizes, t.tiers, t.battery_configs) for t in Template.objects.all()}
    slots = {
        (s.template.system_type, s.key): (s.category.slug, s.qty_rule, s.sort_order, s.label, s.gst_rate, s.is_variable, s.filter_type, s.filter_phase)
        for s in Slot.objects.select_related("template", "category")
    }
    fixed = sorted(
        (f.template.system_type, f.name, f.code, f.unit_price, f.gst_rate, str(f.qty_rule), f.section, f.unit, f.is_tube, f.component_id, f.category_id)
        for f in FixedItem.objects.select_related("template")
    )
    structures = {s.slug: (s.name, s.labour_rate_key) for s in StructureTemplate.objects.all()}
    items = sorted((i.template.slug, i.name, i.item_type, i.tube_size, i.weight_kg, i.unit_price, i.unit, str(i.qty_rule)) for i in StructureTemplateItem.objects.select_related("template"))
    tubes = {t.tube_size: t.weight_kg for t in TubeWeight.objects.all()}
    return {"templates": templates, "slots": slots, "fixed": fixed, "structures": structures, "items": items, "tubes": tubes}


@pytest.fixture
def catalog():
    catalog_fixtures.import_all(order=("bom",))


class TestGoldenray:
    def test_every_row_is_imported_with_its_columns(self, catalog):
        result = legacy_import.import_goldenray_bom(*goldenray_rows())
        assert result["violations"] == []
        assert result["counts"] == {
            "bom_bomtemplate": {"created": 3, "updated": 0, "unchanged": 0, "skipped": 0},
            "bom_bomslot": {"created": 30, "updated": 0, "unchanged": 0, "skipped": 0},
            "bom_bomfixeditem": {"created": 109, "updated": 0, "unchanged": 0, "skipped": 0},
            "bom_structuretemplate": {"created": 3, "updated": 0, "unchanged": 0, "skipped": 0},
            "bom_structuretemplateitem": {"created": 51, "updated": 0, "unchanged": 0, "skipped": 0},
            "bom_tubeweight": {"created": 5, "updated": 0, "unchanged": 0, "skipped": 0},
        }
        hybrid = Template.objects.get(system_type="HYBRID")
        assert hybrid.sizes[0] == {"key": "3", "label": "3 kW"} and hybrid.three_phase_sizes == ["8", "10"] and hybrid.battery_configs == ["0", "1", "2"]
        battery = Slot.objects.get(template=hybrid, key="battery")
        assert battery.qty_rule["type"] == "size_table" and battery.qty_rule["bat_qty"]["2"]["5"] == 2 and "premium_bat_qty" in battery.qty_rule
        assert str(battery.gst_rate) == "0.1800" and battery.sort_order == 5 and battery.label == "Battery"
        assert Slot.objects.get(template=hybrid, key="dcdb").filter_phase == "HYB"
        assert Slot.objects.get(template=hybrid, key="inverter").filter_type == "HYBRID"
        upgrade = Template.objects.get(system_type="UPGRADE")
        assert Slot.objects.get(template=upgrade, key="panel").qty_rule == {"type": "new_panels"}
        assert Slot.objects.get(template=upgrade, key="dc_cable").qty_rule == {"type": "fixed", "qty": 40}
        linked = FixedItem.objects.get(template=upgrade, name="MC4 Connector Pair")
        assert linked.component.sku == "mc4_1" and linked.unit_price is None and linked.qty_rule["type"] == "upgrade_path" and linked.category.slug == "mc4_connector"
        assert FixedItem.objects.get(template=upgrade, name="Meter Box").section == "hybridInv"
        assert FixedItem.objects.filter(template=upgrade, is_tube=True).count() == 2
        mc4 = FixedItem.objects.get(template__system_type="ONGRID", name="MC4 Connector")
        assert str(mc4.unit_price) == "56.00" and mc4.component is None and mc4.qty_rule["bat_lookup"] == "always"
        flat = StructureTemplate.objects.get(slug="flat_roof")
        tube = flat.items.get(name="2.5×1.5 Square Tube 16G")
        assert tube.item_type == "TUBE" and str(tube.weight_kg) == "15.3000" and tube.qty_rule == {"type": "kw_interpolated", "points": {"3": 3, "5": 4, "8": 5, "10": 6}}
        assert StructureTemplate.objects.get(slug="elevated").labour_rate_key == "elevated_structure_rate"
        assert str(TubeWeight.objects.get(tube_size="3x1.5").weight_kg) == "19.0000"
        assert LegacyMap.objects.filter(source_system="BACKEND", source_table="bom_bomfixeditem").count() == 109
        assert AuditLog.objects.filter(action="bom.legacy_import").count() == 1

    def test_rerun_is_idempotent(self, catalog):
        legacy_import.import_goldenray_bom(*goldenray_rows())
        state = _live_state()
        again = legacy_import.import_goldenray_bom(*goldenray_rows())
        assert again["created"] == 0 and again["updated"] == 0 and again["unchanged"] == 201
        assert _live_state() == state

    def test_changed_rows_update_in_place(self, catalog):
        legacy_import.import_goldenray_bom(*goldenray_rows())
        rows = copy.deepcopy(goldenray_rows())
        rows[1][0]["label"] = "Solar Panel (renamed)"
        rows[2][0]["price"] = "60.00"
        result = legacy_import.import_goldenray_bom(*rows)
        assert result["updated"] == 2
        assert Slot.objects.get(template__system_type="ONGRID", key="panel").label == "Solar Panel (renamed)"
        assert Slot.objects.get(template__system_type="ONGRID", key="panel").version == 2

    def test_dry_run_writes_nothing(self, catalog):
        result = legacy_import.import_goldenray_bom(*goldenray_rows(), dry_run=True)
        assert result["created"] == 201
        assert not Template.objects.exists() and not LegacyMap.objects.filter(source_table="bom_bomtemplate").exists()

    def test_bad_rows_are_reported_not_fatal(self, catalog):
        templates, slots, fixed, structures, items, tubes = copy.deepcopy(goldenray_rows())
        templates.append({"id": 99, "system_type": "offgrid", "label": "x", "sizes": {}})
        slots.append({**slots[0], "id": 999, "category_id": 12345})
        slots.append({**slots[1], "id": 998, "template_id": 77})
        fixed.append({**fixed[0], "id": 997, "price": "abc"})
        structures.append({"id": 9, "slug": "ground", "label": "Ground"})
        items.append({**items[0], "id": 996, "item_type": "bolt"})
        tubes.append({"id": 9, "tube_size": "", "weight_kg": "1"})
        slots.append({**slots[2], "id": 995, "qty_mode": "fixed", "filter_type": "weird", "filter_phase": "odd"})
        result = legacy_import.import_goldenray_bom(templates, slots, fixed, structures, items, tubes)
        codes = {(v["source_table"], v["code"], v["severity"]) for v in result["violations"]}
        assert ("bom_bomtemplate", "invalid_value", "error") in codes
        assert ("bom_bomslot", "unknown_category", "error") in codes
        assert ("bom_bomslot", "unknown_template", "error") in codes
        assert ("bom_bomfixeditem", "invalid_value", "error") in codes
        assert ("bom_structuretemplate", "invalid_value", "error") in codes
        assert ("bom_structuretemplateitem", "invalid_value", "error") in codes
        assert ("bom_tubeweight", "invalid_value", "error") in codes
        assert ("bom_bomslot", "qty_mode_ignored", "warning") in codes
        assert ("bom_bomslot", "unknown_filter_type", "warning") in codes and ("bom_bomslot", "unknown_filter_phase", "warning") in codes
        assert Template.objects.count() == 3

    def test_deleted_targets_are_not_recreated(self, catalog):
        legacy_import.import_goldenray_bom(*goldenray_rows())
        Slot.objects.get(template__system_type="ONGRID", key="meter").soft_delete()
        FixedItem.objects.get(template__system_type="ONGRID", name="Cable Tray").soft_delete()
        TubeWeight.objects.get(tube_size="1x1").soft_delete()
        result = legacy_import.import_goldenray_bom(*goldenray_rows())
        assert [v["code"] for v in result["violations"]].count("target_deleted") >= 3
        assert not Slot.objects.filter(template__system_type="ONGRID", key="meter").exists()


class TestFlarize:
    def test_flarize_alone(self, catalog):
        result = legacy_import.import_flarize_bom(flarize_catalog_bom())
        assert [v for v in result["violations"] if v["severity"] == "error"] == []
        assert Template.objects.count() == 3 and PackageProfile.objects.count() == 9
        profile = PackageProfile.objects.get(key="hybrid_value")
        assert profile.battery_included and profile.battery_quantity == 1 and profile.inverter_type == "HYBRID" and str(profile.battery_capacity_kwh) == "10.00"
        assert PackageProfile.objects.get(key="premium").inverter_type == "MICRO"
        mc4 = FixedItem.objects.get(template__system_type="ONGRID", code="fi_mc4_connector")
        assert str(mc4.unit_price) == "56.00"
        ac = Slot.objects.get(template__system_type="UPGRADE", key="ac_cable")
        assert ac.qty_rule == {"type": "fixed", "qty": 20, "hybrid_qty": 30}
        assert FixedItem.objects.get(template__system_type="UPGRADE", name="25mm Battery Cable").unit == "m"
        again = legacy_import.import_flarize_bom(flarize_catalog_bom())
        assert again["created"] == 0 and again["updated"] == 0

    def test_d2_flarize_wins_in_either_order(self, catalog):
        legacy_import.import_goldenray_bom(*goldenray_rows())
        second = legacy_import.import_flarize_bom(flarize_catalog_bom())
        first_order = _live_state()
        assert any(v["code"] == "d2_flarize_wins" for v in second["violations"])
        # the other order, on a fresh copy of the same database state
        for model in (Slot, FixedItem, StructureTemplateItem, Template, StructureTemplate, TubeWeight, PackageProfile):
            model.all_objects.all().delete()
        LegacyMap.objects.filter(source_table__startswith="bom_").exclude(source_table__in=["bom_category", "bom_catalogitem", "bom_itemtier"]).delete()
        LegacyMap.objects.filter(source_table__startswith="catalog.json:bomTemplates").delete()
        LegacyMap.objects.filter(source_table__in=["catalog.json:structureTemplates", "catalog.json:structureTemplates.items", "catalog.json:tubeWeights", "catalog.json:packageProfiles"]).delete()
        legacy_import.import_flarize_bom(flarize_catalog_bom())
        later = legacy_import.import_goldenray_bom(*goldenray_rows())
        assert any(v["code"] == "d2_flarize_wins" for v in later["violations"])
        assert later["created"] == 0
        assert _live_state() == first_order
        ss = FixedItem.objects.get(template__system_type="ONGRID", name="SS Terminal Strip 4P")
        assert str(ss.unit_price) == "150.00"  # catalog.json 150 wins over bom_bomfixeditem 140

    def test_legacy_database_import_helper(self):
        results = import_legacy_database(flarize=True)
        assert results["bom"]["violations"] == [] and results["flarize"]["created"] >= 9
