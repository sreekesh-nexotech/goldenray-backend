"""``/legacy/bom/api/quotation-testimonials/`` — the legacy ``QuotationTestimonialPublicList`` (the active testimonials
in display order, the ``fields = "__all__"`` serializer shape) from ``quotations.services.content.public_testimonials``."""

import datetime as dt
from decimal import Decimal

import pytest

from core.models import LegacyMap
from core.tests.test_deploy import legacy_group
from flarize.cache_utils import bump
from legacy.services.ids import BACKEND, SHIM_ID_OFFSET
from legacy.services.website import DEFAULT_TESTIMONIAL_PHOTO_URL
from legacy.tests.conftest import ordered, throttled
from media.tests.factories import MediaAssetFactory
from quotations.models import Testimonial as QuoteTestimonial
from quotations.services.common import PUBLIC_TESTIMONIALS_NAMESPACE
from quotations.tests.factories import TestimonialFactory as RowFactory

pytestmark = pytest.mark.django_db
URL = "/legacy/bom/api/quotation-testimonials/"
# GET /bom/api/quotation-testimonials/ on the UAT legacy (127.0.0.1:18012), first row, key order included.
LEGACY_ROW = {
    "id": 1,
    "photo_src": "https://golden-ray.b-cdn.net/quotation-v2/assets/a633cb1664c8569f.jpg",
    "monthly_saving": 3000,
    "photo": None,
    "name": "Jose V P",
    "location": "Vadakkal, Alappuzha",
    "system_label": "5 kW System",
    "installed_on": "2025-06-01",
    "quote": "The solar panel installation process was smooth from the very beginning.",
    "quote_ml": "തുടക്കം മുതൽ അവസാനം വരെ മുഴുവൻ പ്രക്രിയയും വളരെ സുഗമമായിരുന്നു.",
    "photo_url": "",
    "bill_before": 3200,
    "bill_after": 200,
    "is_active": True,
    "sort_order": 1,
    "created_at": "2026-09-28T23:09:24.009207+05:30",
    "updated_at": "2026-09-28T23:09:24.009220+05:30",
}


def _imported(**extra) -> QuoteTestimonial:
    """The UAT row as ``quotations.services.legacy_import.import_backend_testimonials`` stores it."""
    values = {
        "customer_name": "Jose V P",
        "location": "Vadakkal, Alappuzha",
        "capacity_kw": Decimal("5"),
        "system_label": "5 kW System",
        "installed_on": dt.date(2025, 6, 1),
        "quote_en": LEGACY_ROW["quote"],
        "quote_ml": LEGACY_ROW["quote_ml"],
        "bill_before": Decimal("3200"),
        "bill_after": Decimal("200"),
        "sort_order": 1,
        **extra,
    }
    row = RowFactory(**values)
    stamp = dt.datetime(2026, 9, 28, 17, 39, 24, 9207, tzinfo=dt.UTC)
    QuoteTestimonial.all_objects.filter(pk=row.pk).update(created_at=stamp, updated_at=stamp + dt.timedelta(microseconds=13))
    LegacyMap.objects.create(source_system=BACKEND, source_table="bom_quotationtestimonial", source_id="1", target_table="quotations_testimonial", target_id=row.pk)
    return row


def test_the_imported_row_answers_the_legacy_body_byte_for_byte(api_client, django_assert_max_num_queries):
    _imported()
    with django_assert_max_num_queries(3):
        response = api_client.get(URL)
    assert response.status_code == 200
    assert ordered(response) == [LEGACY_ROW] and list(ordered(response)[0]) == list(LEGACY_ROW)


def test_only_active_website_rows_in_display_order_and_platform_rows_get_shim_ids(api_client, django_assert_max_num_queries):
    later = RowFactory(sort_order=5, bill_before=None, bill_after=None)
    first = RowFactory(sort_order=0, photo_url="https://cdn.example/p.jpg", bill_before=Decimal("2500.00"), bill_after=Decimal("300.00"))
    RowFactory(sort_order=1, is_active=False)
    RowFactory(sort_order=2, show_on_website=False)
    deleted = RowFactory(sort_order=3)
    QuoteTestimonial.objects.filter(pk=deleted.pk).update(deleted_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    for _ in range(3):
        RowFactory(sort_order=9)
    with django_assert_max_num_queries(3):
        body = ordered(api_client.get(URL))
    assert [row["id"] for row in body[:2]] == [SHIM_ID_OFFSET + first.pk, SHIM_ID_OFFSET + later.pk] and len(body) == 5
    assert (body[0]["photo_src"], body[0]["photo_url"], body[0]["bill_before"], body[0]["bill_after"], body[0]["monthly_saving"]) == (
        "https://cdn.example/p.jpg",
        "https://cdn.example/p.jpg",
        2500,
        300,
        2200,
    )
    # the legacy columns were NOT NULL with defaults 3200 / 200: an unknown bill prints the legacy defaults
    assert (body[1]["photo_src"], body[1]["bill_before"], body[1]["bill_after"], body[1]["monthly_saving"]) == (DEFAULT_TESTIMONIAL_PHOTO_URL, 3200, 200, 3000)


def test_an_uploaded_public_photo_wins(api_client):
    asset = MediaAssetFactory(visibility="PUBLIC", cdn_url="https://cdn.example/jose.webp")
    _imported(photo=asset, photo_url="https://cdn.example/other.jpg")
    row = api_client.get(URL).json()[0]
    assert row["photo"] == row["photo_src"] == "https://cdn.example/jose.webp" and row["photo_url"] == "https://cdn.example/other.jpg"


def test_cached_under_the_testimonials_namespace(api_client):
    row = _imported()
    assert api_client.get(URL).json()[0]["name"] == "Jose V P"
    QuoteTestimonial.objects.filter(pk=row.pk).update(customer_name="Jose")
    assert api_client.get(URL).json()[0]["name"] == "Jose V P"
    bump(PUBLIC_TESTIMONIALS_NAMESPACE)
    assert api_client.get(URL).json()[0]["name"] == "Jose"


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_writes_are_not_shimmed(api_client, method):
    response = getattr(api_client, method)(URL, {}, format="json")
    assert response.status_code == 405 and response.json() == {"detail": f'Method "{method.upper()}" not allowed.'}


def test_slashless_url_redirects_and_throttle_is_public_read(api_client, settings):
    response = api_client.get("/legacy/bom/api/quotation-testimonials")
    assert response.status_code == 301 and response["Location"] == "/bom/api/quotation-testimonials/"
    throttled(settings, "public_read", "2/min")
    assert [api_client.get(URL).status_code for _ in range(3)][-1] == 429


def test_nginx_routes_the_public_read_to_the_shim_and_the_manage_urls_to_the_old_container():
    assert legacy_group("/bom/api/quotation-testimonials/") == legacy_group("/bom/api/quotation-testimonials") == "bom_public"
    assert legacy_group("/bom/api/quotation-testimonials/manage/") == "bom_other"
