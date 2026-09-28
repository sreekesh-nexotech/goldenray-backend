"""seo.services.legacy_import: goldenray_metadata (with value parity against the legacy /api/metadata/) and the CMS
siteconfig SEO defaults.

``fixtures/legacy_backend/tables.json`` is the read-only export of ``goldenray_metadata``
(``blog/tests/fixtures/export_legacy_tables.py``); ``metadata_list.json`` is the legacy ``GET /api/metadata/`` response.
"""

import copy
import json
from pathlib import Path

import pytest

from audit.models import AuditLog
from company.models import CompanyProfile
from core.models import LegacyMap
from media.tests.factories import MediaAssetFactory
from seo.models import PageMetadata
from seo.services import legacy_import

pytestmark = pytest.mark.django_db
FIXTURES = Path(__file__).parent / "fixtures" / "legacy_backend"
ROWS = json.loads((FIXTURES / "tables.json").read_text(encoding="utf-8"))["goldenray_metadata"]
LEGACY_LIST = json.loads((FIXTURES / "metadata_list.json").read_text(encoding="utf-8"))


def test_import_is_idempotent_and_traceable():
    report = legacy_import.import_page_metadata(ROWS)
    assert report == {"created": 11, "updated": 0, "skipped": 0, "violations": []}
    assert LegacyMap.objects.filter(source_system="BACKEND", source_table="goldenray_metadata").count() == 11
    again = legacy_import.import_page_metadata(ROWS)
    assert again == {"created": 0, "updated": 0, "skipped": 11, "violations": []} and PageMetadata.objects.count() == 11
    changed = copy.deepcopy(ROWS)
    changed[0]["title"] = "New home title"
    assert legacy_import.import_page_metadata(changed)["updated"] == 1
    assert AuditLog.objects.filter(action="seo.legacy_import").count() == 3


def test_public_endpoint_serves_every_legacy_item(api_client):
    legacy_import.import_page_metadata(ROWS)
    for item in LEGACY_LIST:
        response = api_client.get(f"/api/public/v1/seo/metadata/{item['page']}/")
        assert response.status_code == 200, item["page"]
        expected = {key: value for key, value in item.items() if key != "id"}
        assert response.json() == expected


def test_violations():
    rows = copy.deepcopy(ROWS[:3])
    rows[0]["ogtype"] = "Web Site!"
    rows[1]["keywords"] = "solar, kerala , "
    rows[2]["page"] = "bad page"
    rows.append({**ROWS[3], "id": 99, "page": ROWS[0]["page"]})
    rows.append({**ROWS[4], "id": 98, "keywords": 5})
    report = legacy_import.import_page_metadata(rows)
    assert sorted(v["code"] for v in report["violations"]) == ["keywords_invalid", "og_type_invalid", "page_clash", "page_invalid"]
    assert PageMetadata.objects.get(page=ROWS[1]["page"]).keywords == ["solar", "kerala"]
    assert PageMetadata.objects.get(page=ROWS[0]["page"]).og_type == "website"
    assert report["created"] == 3 and report["skipped"] == 2


def test_site_seo_defaults_go_to_the_company_profile():
    asset = MediaAssetFactory()
    row = {"id": 1, "default_meta_description": "Solar for Kerala homes.", "default_og_image_id": 7}
    report = legacy_import.import_site_seo_defaults(row, media_map={7: asset})
    assert report["created"] == 1 and report["violations"] == []
    profile = CompanyProfile.objects.get()
    assert profile.default_meta_description == "Solar for Kerala homes." and profile.default_og_image == asset
    assert LegacyMap.objects.get(source_table="siteconfig_settings").target_id == profile.pk
    assert legacy_import.import_site_seo_defaults(row, media_map={7: asset})["skipped"] == 1


def test_site_seo_defaults_with_nothing_to_import_and_unmapped_media():
    fixture = json.loads((Path(__file__).parents[2] / "blog" / "tests" / "fixtures" / "legacy_cms" / "tables.json").read_text(encoding="utf-8"))["siteconfig_settings"][0]
    assert legacy_import.import_site_seo_defaults(fixture) == {"created": 0, "updated": 0, "skipped": 1, "violations": []}
    assert not CompanyProfile.objects.exists()
    report = legacy_import.import_site_seo_defaults({**fixture, "default_og_image_id": 3})
    assert [v["code"] for v in report["violations"]] == ["media_unmapped"]
    asset = MediaAssetFactory()
    LegacyMap.objects.create(source_system="CMS", source_table="media_asset", source_id="3", target_table="media_asset", target_id=asset.pk)
    assert legacy_import.import_site_seo_defaults({**fixture, "default_og_image_id": 3})["created"] == 1
