"""catalog/components/import/ (dry run → token → commit) and catalog/components/export/."""

import csv
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from audit.models import AuditLog
from catalog.models import Brand, Component
from catalog.services import csv_io
from catalog.tests.factories import BatteryFamilyFactory, BrandFactory, CategoryFactory, ComponentTierFactory, battery, inverter, panel, panel_category

pytestmark = pytest.mark.django_db
IMPORT = "/api/v1/catalog/components/import/"
EXPORT = "/api/v1/catalog/components/export/"


def upload(text: str, name: str = "components.csv") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


def rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


PANEL_CSV = (
    "sku,category,brand,name,tiers,is_public,spec.wattage_w,spec.efficiency_pct,spec.certifications\n"
    ',panel,Waaree,Waaree 540W,BASE|VALUE,true,540,21.36,"[""BIS""]"\n'
    "p9,panel,Adani,Adani 620W,PREMIUM,,620,,\n"
)


class TestPermissions:
    def test_anonymous_and_forbidden(self, api_client, viewer, outsider):
        assert api_client.get(EXPORT).status_code == 401
        assert api_client.post(IMPORT, {"file": upload(PANEL_CSV)}, format="multipart").status_code == 401
        assert outsider.get(EXPORT).status_code == 403
        assert viewer.get(EXPORT).status_code == 200
        assert viewer.post(IMPORT, {"file": upload(PANEL_CSV)}, format="multipart").status_code == 403

    def test_updates_need_edit(self, auth_client, make_user):
        panel(sku="p1")
        creator = auth_client(make_user(grants={"catalog": ["view", "create"]}))
        report = creator.post(IMPORT, {"file": upload("sku,category,name\np1,panel,Renamed\n")}, format="multipart").json()
        assert report["rows"][0]["action"] == "error" and "catalog.edit" in report["rows"][0]["errors"]["sku"][0]


class TestImport:
    def test_dry_run_then_commit(self, client, catalog_user):
        panel_category()
        dry = client.post(IMPORT, {"file": upload(PANEL_CSV)}, format="multipart")
        assert dry.status_code == 200, dry.json()
        report = dry.json()
        assert report["dry_run"] is True and report["committed"] is False and report["summary"] == {"rows": 2, "create": 2, "update": 0, "unchanged": 0, "error": 0}
        assert report["rows"][0]["sku"] == "PNL-0001" and report["rows"][0]["brand_created"] == "Waaree" and report["import_token"]
        assert not Component.objects.exists() and not Brand.objects.exists()
        missing_token = client.post(IMPORT, {"file": upload(PANEL_CSV), "dry_run": "false"}, format="multipart")
        assert missing_token.status_code == 400 and missing_token.json()["code"] == "dry_run_required"
        other_file = client.post(IMPORT, {"file": upload(PANEL_CSV + "\n"), "dry_run": "false", "import_token": report["import_token"]}, format="multipart")
        assert other_file.json()["code"] == "dry_run_required"
        commit = client.post(IMPORT, {"file": upload(PANEL_CSV), "dry_run": "false", "import_token": report["import_token"]}, format="multipart")
        assert commit.status_code == 200 and commit.json()["committed"] is True
        created = Component.objects.get(sku="PNL-0001")
        assert created.status == "DRAFT" and created.is_public and created.panel_spec.certifications == ["BIS"] and sorted(created.tiers.values_list("tier", flat=True)) == ["BASE", "VALUE"]
        assert Component.objects.get(sku="p9").panel_spec.wattage_w == 620
        assert AuditLog.objects.get(action="catalog.components_imported").after["create"] == 2

    def test_token_is_bound_to_the_user(self, client, auth_client, make_user):
        panel_category()
        token = client.post(IMPORT, {"file": upload(PANEL_CSV)}, format="multipart").json()["import_token"]
        other = auth_client(make_user(grants={"catalog": "*"}))
        assert other.post(IMPORT, {"file": upload(PANEL_CSV), "dry_run": "false", "import_token": token}, format="multipart").json()["code"] == "dry_run_required"

    def test_row_errors_block_the_commit(self, client, catalog_user):
        panel_category()
        bad = PANEL_CSV + "p1,nowhere,,X,,,,,\np2,panel,,Y,GOLD,,540,,\np3,panel,,Z,,,,,\np4,panel,,W,,,abc,,\np9,panel,,Dup,,,540,,\n"
        report = client.post(IMPORT, {"file": upload(bad)}, format="multipart").json()
        errors = {row["sku"]: row["errors"] for row in report["rows"] if row["action"] == "error"}
        assert set(errors) == {"p1", "p2", "p4", "p9"} and report["import_token"] is None  # p3: a DRAFT panel without spec is fine
        assert "category" in errors["p1"] and "tiers" in errors["p2"] and "panel_spec" in errors["p4"] and "sku" in errors["p9"]
        token = csv_io.issue_token(bad.encode(), catalog_user)  # as if the rows had been fixed in between
        committed = client.post(IMPORT, {"file": upload(bad), "dry_run": "false", "import_token": token}, format="multipart")
        assert committed.status_code == 400 and committed.json()["code"] == "import_has_errors" and not Component.objects.exists()

    def test_updates_and_unchanged_rows(self, client):
        component = panel(sku="p1", name="Old", spec={"wattage_w": 540})
        text = "sku,category,name,spec.wattage_w\np1,panel,New,545\n"
        report = client.post(IMPORT, {"file": upload(text)}, format="multipart").json()
        assert report["rows"][0]["action"] == "update"
        client.post(IMPORT, {"file": upload(text), "dry_run": "false", "import_token": report["import_token"]}, format="multipart")
        component.refresh_from_db()
        assert component.name == "New" and component.panel_spec.wattage_w == 545
        assert client.post(IMPORT, {"file": upload(text)}, format="multipart").json()["rows"][0]["action"] == "unchanged"

    @pytest.mark.parametrize(
        "content,code",
        [
            (b"\xff\xfe\x00bad", "invalid_csv"),
            (b"name,category\nx,panel\n", "invalid_csv"),
            (b"sku,category,name,colour\nx,panel,n,red\n", "invalid_csv"),
            (b"sku,category,name\nx,panel,n,extra\n", "invalid_csv"),
        ],
    )
    def test_file_level_errors(self, client, content, code):
        response = client.post(IMPORT, {"file": SimpleUploadedFile("c.csv", content)}, format="multipart")
        assert response.status_code == 400 and response.json()["code"] == code

    def test_limits(self, client, monkeypatch):
        monkeypatch.setattr(csv_io, "MAX_ROWS", 1)
        response = client.post(IMPORT, {"file": upload("sku,category,name\na,panel,x\nb,panel,y\n")}, format="multipart")
        assert response.json()["code"] == "invalid_csv"
        monkeypatch.setattr(csv_io, "MAX_BYTES", 10)
        assert client.post(IMPORT, {"file": upload(PANEL_CSV)}, format="multipart").status_code == 413

    def test_validation_envelope(self, client):
        response = client.post(IMPORT, {}, format="multipart")
        assert response.status_code == 400 and "file" in response.json()["errors"]


class TestExport:
    def test_round_trip_is_lossless(self, client):
        family = BatteryFamilyFactory(slug="seg-lv")
        brand = BrandFactory(name="=cmd")
        first = panel(sku="p1", brand=brand, brand_label="=cmd", name="-Panel", spec={"wattage_w": 540, "certifications": ["BIS", "IEC"]})
        ComponentTierFactory(component=first, tier="VALUE")
        inverter(sku="i1", spec={"compatible_battery_families": ["seg-lv"]})
        battery(sku="b1", spec={"family": family, "compatible_inverters": None, "capacity_kwh": "5.12"})
        CategoryFactory(slug="dcdb")
        response = client.get(EXPORT)
        assert response.status_code == 200 and response["Content-Type"].startswith("text/csv") and "attachment" in response["Content-Disposition"]
        text = response.content.decode()
        exported = {row["sku"]: row for row in rows(text)}
        assert exported["p1"]["brand"] == "'=cmd" and exported["p1"]["name"] == "'-Panel" and exported["p1"]["tiers"] == "VALUE"
        assert exported["p1"]["spec.certifications"] == '["BIS", "IEC"]' and exported["b1"]["spec.family"] == "seg-lv" and exported["b1"]["spec.compatible_inverters"] == ""
        report = client.post(IMPORT, {"file": upload(text)}, format="multipart").json()
        assert report["summary"]["unchanged"] == 3 and report["summary"]["error"] == 0, report

    def test_filters_and_limit(self, client, monkeypatch):
        panel(sku="p1")
        inverter(sku="i1")
        assert [row["sku"] for row in rows(client.get(EXPORT, {"category": "inverter"}).content.decode())] == ["i1"]
        assert client.get(EXPORT, HTTP_ACCEPT="text/csv").status_code == 200
        monkeypatch.setattr(csv_io, "MAX_EXPORT_ROWS", 1)
        response = client.get(EXPORT)
        assert response.status_code == 400 and response.json()["code"] == "export_too_large"
