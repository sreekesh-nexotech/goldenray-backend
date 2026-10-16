"""The exact request sequence the Studio's Catalog tab now sends.

Pins the contract the frontend depends on: a bare create lands DRAFT, a blank
spec is omitted rather than sent empty, and Activate is what insists on the
spec. If any of these change, the tab's buttons start producing 400s.
"""
import pytest
from catalog.models.component import ComponentStatus
from catalog.tests.factories import CategoryFactory

URL = "/api/v1/catalog/components/"
CAT_URL = "/api/v1/catalog/categories/"


@pytest.mark.django_db
class TestStudioCatalogCreate:
    def test_bare_create_lands_draft_with_a_generated_sku(self, client):
        """What 'Add product' sends for a category with no spec: category + name."""
        cat = CategoryFactory(bom_role="MISC", sku_prefix="MSC")
        r = client.post(URL, {"category": str(cat.uid), "name": "Cable Tie"}, format="json")
        assert r.status_code == 201, r.json()
        body = r.json()
        assert body["status"] == ComponentStatus.DRAFT
        assert body["sku"] == "MSC-0001"
        assert body["tiers"] == []

    def test_a_tierless_draft_activates_but_a_panel_without_wattage_cannot(self, client):
        """The two separate 'invisible to quotes' states, and which one blocks Activate."""
        misc = CategoryFactory(bom_role="MISC", sku_prefix="MSC")
        r = client.post(URL, {"category": str(misc.uid), "name": "Lug"}, format="json")
        uid, version = r.json()["uid"], r.json()["version"]
        act = client.post(f"{URL}{uid}/activate/", {"expected_version": version}, format="json")
        assert act.status_code == 200 and act.json()["status"] == ComponentStatus.ACTIVE

        panel_cat = CategoryFactory(bom_role="MAIN_PANEL", sku_prefix="PNL")
        p = client.post(URL, {"category": str(panel_cat.uid), "name": "Waaree 540"}, format="json")
        assert p.status_code == 201, p.json()
        assert p.json()["panel_spec"] is None
        blocked = client.post(f"{URL}{p.json()['uid']}/activate/", {"expected_version": p.json()["version"]}, format="json")
        assert blocked.status_code == 409, blocked.json()
        assert blocked.json()["code"] == "spec_required"

    def test_sending_a_blank_spec_is_rejected_which_is_why_the_tab_omits_it(self, client):
        """Why `isBlankSpec` exists: an empty spec object is a 400, not a no-op."""
        cat = CategoryFactory(bom_role="MAIN_PANEL", sku_prefix="PNL")
        r = client.post(URL, {"category": str(cat.uid), "name": "P", "panel_spec": {"voc_v": "49.5"}}, format="json")
        assert r.status_code == 400
        assert "wattage_w" in r.json()["errors"]["panel_spec"]

    def test_panel_and_inverter_create_with_their_required_spec_then_activate(self, client):
        """The happy path for both spec kinds, including inverter_type."""
        panel_cat = CategoryFactory(bom_role="MAIN_PANEL", sku_prefix="PNL")
        p = client.post(
            URL,
            {"category": str(panel_cat.uid), "name": "Waaree 540", "tiers": ["BASE"], "panel_spec": {"wattage_w": 540, "voc_v": "49.5"}},
            format="json",
        )
        assert p.status_code == 201, p.json()
        ok = client.post(f"{URL}{p.json()['uid']}/activate/", {"expected_version": p.json()["version"]}, format="json")
        assert ok.status_code == 200 and ok.json()["status"] == ComponentStatus.ACTIVE

        inv_cat = CategoryFactory(bom_role="MAIN_INVERTER", sku_prefix="INV")
        missing_type = client.post(URL, {"category": str(inv_cat.uid), "name": "G5", "inverter_spec": {"kw": "5"}}, format="json")
        assert missing_type.status_code == 400
        assert "inverter_type" in missing_type.json()["errors"]["inverter_spec"]

        i = client.post(
            URL,
            {"category": str(inv_cat.uid), "name": "Growatt 5k", "inverter_spec": {"kw": "5", "inverter_type": "ONGRID"}},
            format="json",
        )
        assert i.status_code == 201, i.json()
        ok = client.post(f"{URL}{i.json()['uid']}/activate/", {"expected_version": i.json()["version"]}, format="json")
        assert ok.status_code == 200

    def test_new_type_form_payload_creates_a_category_that_accepts_components(self, client):
        """Exactly the body NewCategoryForm sends, then a product under it."""
        r = client.post(
            CAT_URL,
            {"slug": "fuse-box", "name": "Fuse Box", "bom_role": "PROTECTION", "gst_rate": "0.18", "unit": "NOS", "sku_prefix": "FUS"},
            format="json",
        )
        assert r.status_code == 201, r.json()
        assert r.json()["spec_kind"] is None
        c = client.post(URL, {"category": str(r.json()["uid"]), "name": "Fuse 32A"}, format="json")
        assert c.status_code == 201 and c.json()["sku"] == "FUS-0001"

    def test_gst_as_a_percentage_is_refused_not_silently_applied(self, client):
        """The hundredfold error the form checks for client-side as well."""
        r = client.post(
            CAT_URL,
            {"slug": "bad", "name": "Bad", "gst_rate": "18", "sku_prefix": "BAD"},
            format="json",
        )
        assert r.status_code == 400 and "gst_rate" in r.json()["errors"]

    def test_stale_activate_is_refused(self, client):
        """The tab disables Activate while a row is dirty; this is the server-side half."""
        cat = CategoryFactory(bom_role="MISC", sku_prefix="MSC")
        r = client.post(URL, {"category": str(cat.uid), "name": "Lug"}, format="json")
        uid = r.json()["uid"]
        stale = client.post(f"{URL}{uid}/activate/", {"expected_version": r.json()["version"] + 5}, format="json")
        assert stale.status_code == 409
