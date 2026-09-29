"""Old main-backend reads that are not lists or calculators: ``/api/metadata/``, ``/api/installation-stats/``,
``/bom/api/quotation-settings/`` and the ``/bom/api/calculate/`` quote.

* ``metadata/``: every ``seo_page_metadata`` row in the legacy serializer shape (``imageUrl``, ``ogtype``), legacy ids.
* ``installation-stats/``: ``leads.services.installations.installation_stats`` (the same keys, in order).
* ``quotation-settings/``: the company profile's quotation-offer columns in the legacy ``QuotationSettings`` shape
  (docs/decisions/foundation-f3.md); the computed ``offer_active`` / ``offer_image_src`` are
  ``company.services.profile.quotation_offer``; the ``*_ml`` columns are the stored values (no English fallback, as
  the legacy serializer printed them).
* ``calculate/``: ``bom.services.website_quote.quote`` filtered through ``public_body`` (business default B-1: no
  ``cost_breakdown`` / ``totals``).
* ``quotation-testimonials/``: ``quotations.services.content.public_testimonials`` (active, shown on the website, in
  display order) in the legacy ``QuotationTestimonialSerializer`` shape (``fields = "__all__"``): legacy ids, the
  uploaded photo → configured URL → stock photo ``photo_src``, whole-rupee bills (the legacy NOT NULL defaults 3200 /
  200 where the platform row has none).
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers

from bom.services import website_quote
from company.services import profile as company_profile
from leads.services import installations
from legacy.services.ids import BACKEND, legacy_ids
from quotations.models import Testimonial
from quotations.services import content as quotation_content
from seo.models import PageMetadata
from seo.services import metadata as seo_metadata

_DATETIME = serializers.DateTimeField()
_DATE = serializers.DateField()


def metadata_list() -> list[dict]:
    rows = list(seo_metadata.metadata_queryset())
    ids = legacy_ids(PageMetadata, [row.pk for row in rows], system=BACKEND, table="goldenray_metadata")
    out = [
        {
            "id": ids[row.pk],
            "page": row.page,
            "title": row.title,
            "description": row.description,
            "keywords": list(row.keywords),
            "imageUrl": seo_metadata.image_url(row),
            "ogtype": row.og_type,
        }
        for row in rows
    ]
    return sorted(out, key=lambda item: item["id"])


def installation_stats(pincode: str) -> dict:
    return dict(installations.installation_stats(pincode))


def _date(value):
    return None if value is None else _DATE.to_representation(value)


def quotation_settings() -> dict:
    profile = company_profile.current_profile()
    offer = company_profile.quotation_offer(profile, timezone.localdate())
    image = profile.quotation_offer_image
    uploaded = image.cdn_url if image is not None and image.deleted_at is None and image.is_public and image.cdn_url else None
    return {
        "offer_active": offer.active,
        "offer_image_src": offer.image_src,
        "offer_image": uploaded,
        "offer_enabled": profile.quotation_offer_enabled,
        "offer_title": profile.quotation_offer_title,
        "offer_description": profile.quotation_offer_description,
        "offer_details": profile.quotation_offer_details,
        "offer_title_ml": profile.quotation_offer_title_ml,
        "offer_description_ml": profile.quotation_offer_description_ml,
        "offer_details_ml": profile.quotation_offer_details_ml,
        "offer_valid_from": _date(profile.quotation_offer_valid_from),
        "offer_valid_until": _date(profile.quotation_offer_valid_until),
        "offer_image_url": profile.quotation_offer_image_url,
        "updated_at": None if profile.pk is None or profile.updated_at is None else _DATETIME.to_representation(profile.updated_at),
    }


def bom_quote(body) -> dict:
    return website_quote.public_body(website_quote.quote(body, today=timezone.localdate()))


# ── quotation-testimonials ───────────────────────────────────────────────────────────────────────────────────────
DEFAULT_TESTIMONIAL_PHOTO_URL = "https://golden-ray.b-cdn.net/quotation-v2/assets/a633cb1664c8569f.jpg"
LEGACY_BILL_BEFORE, LEGACY_BILL_AFTER = 3200, 200


def _rupees(value, default: int) -> int:
    return default if value is None else int(value)


def testimonials() -> list[dict]:
    rows = list(quotation_content.public_testimonials())
    ids = legacy_ids(Testimonial, [row.pk for row in rows], system=BACKEND, table="bom_quotationtestimonial")
    out = []
    for row in rows:
        photo = row.photo if row.photo_id and row.photo.deleted_at is None and row.photo.is_public and row.photo.cdn_url else None
        before, after = _rupees(row.bill_before, LEGACY_BILL_BEFORE), _rupees(row.bill_after, LEGACY_BILL_AFTER)
        out.append(
            {
                "id": ids[row.pk],
                "photo_src": photo.cdn_url if photo else (row.photo_url or DEFAULT_TESTIMONIAL_PHOTO_URL),
                "monthly_saving": max(0, before - after),
                "photo": photo.cdn_url if photo else None,
                "name": row.customer_name,
                "location": row.location,
                "system_label": row.system_label,
                "installed_on": _date(row.installed_on),
                "quote": row.quote_en,
                "quote_ml": row.quote_ml,
                "photo_url": row.photo_url,
                "bill_before": before,
                "bill_after": after,
                "is_active": row.is_active,
                "sort_order": row.sort_order,
                "created_at": _DATETIME.to_representation(row.created_at),
                "updated_at": _DATETIME.to_representation(row.updated_at),
            }
        )
    return out
