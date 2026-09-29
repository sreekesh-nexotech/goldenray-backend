"""FAQs in the staff SEO overview (convention: ``<app>/services/seo_overview.py``, see ``seo.services``).

Every live FAQ that is not archived. A FAQ carries its own SEO fields (``SeoFields``); its title falls back to the
question and its path is the route of the page it appears on (``Faq.seo_path``; blank when it has no page).
"""

from __future__ import annotations

from django.db.models import CharField, F, Value
from django.db.models.functions import Coalesce

from faqs.models import Faq
from seo.services.overview import overview_rows

SEO_OVERVIEW_KIND = "faq"


def seo_overview_rows():
    return overview_rows(
        Faq.objects.exclude(status=Faq.Status.ARCHIVED).order_by(),  # no model ordering inside the UNION ALL
        kind=SEO_OVERVIEW_KIND,
        label=F("question"),
        path=Coalesce(F("page__route"), Value(""), output_field=CharField()),
        record_status=F("status"),
        seo_title=F("seo_title"),
        meta_description=F("meta_description"),
        fallback_title=F("question"),
        schema_type=F("schema_type"),
        noindex=F("noindex"),
    )
