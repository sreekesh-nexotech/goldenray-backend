"""``/legacy/bom/api/quotation-settings/`` (the legacy ``QuotationSettings`` serializer from the company profile), the
B-1 filter of the public quote, and the legacy-id helpers."""

import datetime as dt
from unittest import mock

import pytest

from bom.services.website_quote import INTERNAL_KEYS, public_body
from company.tests.factories import CompanyProfileFactory
from core.models import LegacyMap
from legacy.services.ids import BACKEND, SHIM_ID_OFFSET, legacy_id, legacy_ids, resolve_pk
from legacy.tests.conftest import ordered
from media.tests.factories import MediaAssetFactory
from reference.models import DeviceType
from reference.tests.factories import DeviceTypeFactory

pytestmark = pytest.mark.django_db
URL = "/legacy/bom/api/quotation-settings/"
# The UAT legacy answer (GET /bom/api/quotation-settings/ on 127.0.0.1:18012), key order included.
LEGACY_KEYS = [
    "offer_active",
    "offer_image_src",
    "offer_image",
    "offer_enabled",
    "offer_title",
    "offer_description",
    "offer_details",
    "offer_title_ml",
    "offer_description_ml",
    "offer_details_ml",
    "offer_valid_from",
    "offer_valid_until",
    "offer_image_url",
    "updated_at",
]


def _uat_profile(**extra):
    values = {
        "quotation_offer_enabled": True,
        "quotation_offer_title": "Priority 10-Day Installation",
        "quotation_offer_description": "Fast-tracked scheduling and execution",
        "quotation_offer_details": "",
        "quotation_offer_title_ml": "10 ദിവസത്തിനുള്ളിൽ മുൻഗണനാ ഇൻസ്റ്റലേഷൻ",
        "quotation_offer_description_ml": "",
        "quotation_offer_details_ml": "",
        "quotation_offer_valid_from": dt.date(2026, 1, 1),
        "quotation_offer_valid_until": dt.date(2026, 12, 31),
        "quotation_offer_image_url": "https://golden-ray.b-cdn.net/icons/37.png",
    }
    return CompanyProfileFactory(**{**values, **extra})


def test_quotation_settings_is_the_legacy_serializer(api_client, django_assert_max_num_queries):
    profile = _uat_profile()
    with mock.patch("django.utils.timezone.localdate", return_value=dt.date(2026, 9, 29)), django_assert_max_num_queries(4):
        response = api_client.get(URL)
    body = ordered(response)
    assert list(body) == LEGACY_KEYS and response["Cache-Control"] == "public, max-age=60"
    assert body["offer_active"] is True and body["offer_image_src"] == "https://golden-ray.b-cdn.net/icons/37.png" and body["offer_image"] is None
    assert body["offer_description_ml"] == ""  # the stored value, no English fallback (the legacy serializer printed the column)
    assert (body["offer_valid_from"], body["offer_valid_until"]) == ("2026-01-01", "2026-12-31")
    assert body["updated_at"].startswith(profile.updated_at.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30))).isoformat()[:19])


def test_offer_outside_its_dates_is_inactive_and_an_uploaded_image_wins(api_client):
    asset = MediaAssetFactory(visibility="PUBLIC", cdn_url="https://cdn.example/offer.png")
    _uat_profile(quotation_offer_valid_until=dt.date(2026, 1, 31), quotation_offer_image=asset)
    with mock.patch("django.utils.timezone.localdate", return_value=dt.date(2026, 9, 29)):
        body = api_client.get(URL).json()
    assert body["offer_active"] is False and body["offer_image"] == body["offer_image_src"] == "https://cdn.example/offer.png"


def test_quotation_settings_without_a_profile_serves_the_defaults(api_client):
    body = api_client.get(URL).json()
    assert list(body) == LEGACY_KEYS and body["updated_at"] is None and body["offer_image"] is None


def test_public_body_drops_only_the_internal_keys():
    full = {"bom_lines": [1], "cost_breakdown": {"install": 1}, "totals": {"margin": 1}, "pricing": {"x": 1}, "meta": {}, "available_offers": []}
    assert list(public_body(full)) == ["bom_lines", "pricing", "meta", "available_offers"] and INTERNAL_KEYS == ("cost_breakdown", "totals")


def test_legacy_ids_prefer_the_map_and_fall_back_to_the_offset():
    mapped, new = DeviceTypeFactory(), DeviceTypeFactory()
    LegacyMap.objects.create(source_system=BACKEND, source_table="device_types", source_id="16", target_table="reference_device_type", target_id=mapped.pk)
    assert legacy_ids(DeviceType, [mapped.pk, new.pk], system=BACKEND, table="device_types") == {mapped.pk: 16, new.pk: SHIM_ID_OFFSET + new.pk}
    assert legacy_id(DeviceType, new.pk, system=BACKEND, table="device_types") == SHIM_ID_OFFSET + new.pk
    assert resolve_pk(DeviceType, 16, system=BACKEND, table="device_types") == mapped.pk
    assert resolve_pk(DeviceType, SHIM_ID_OFFSET + new.pk, system=BACKEND, table="device_types") == new.pk
    assert resolve_pk(DeviceType, 17, system=BACKEND, table="device_types") is None


def test_a_non_numeric_source_id_falls_back_to_the_offset():
    row = DeviceTypeFactory()
    LegacyMap.objects.create(source_system=BACKEND, source_table="device_types", source_id="abc", target_table="reference_device_type", target_id=row.pk)
    assert legacy_ids(DeviceType, [row.pk], system=BACKEND, table="device_types") == {row.pk: SHIM_ID_OFFSET + row.pk}
