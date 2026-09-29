"""Job positions in the staff SEO overview (convention: ``<app>/services/seo_overview.py``, see ``seo.services``).

Every live position that is not archived. A position carries its own SEO fields (``SeoFields``); its title falls back
to the job title and its path is ``/career/<slug>`` (``JobPosition.seo_path``).
"""

from __future__ import annotations

from django.db.models import CharField, F, Value
from django.db.models.functions import Concat

from careers.models import JobPosition
from seo.services.overview import overview_rows

SEO_OVERVIEW_KIND = "job"


def seo_overview_rows():
    return overview_rows(
        JobPosition.objects.exclude(status=JobPosition.Status.ARCHIVED).order_by(),  # no model ordering inside the UNION ALL
        kind=SEO_OVERVIEW_KIND,
        label=F("title"),
        path=Concat(Value("/career/"), F("slug"), output_field=CharField()),
        record_status=F("status"),
        seo_title=F("seo_title"),
        meta_description=F("meta_description"),
        fallback_title=F("title"),
        schema_type=F("schema_type"),
        noindex=F("noindex"),
    )
