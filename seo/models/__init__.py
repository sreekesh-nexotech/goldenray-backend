"""SEO models: the shared SeoFields mixin, page metadata and redirects (the SEO overview is computed, not stored)."""

from seo.models.fields import DESCRIPTION_MAX, DESCRIPTION_MIN, TITLE_MAX, SchemaType, SeoFields, seo_issues_for, seo_status_for
from seo.models.metadata import PageMetadata
from seo.models.redirect import Redirect

__all__ = ["DESCRIPTION_MAX", "DESCRIPTION_MIN", "PageMetadata", "Redirect", "SchemaType", "SeoFields", "TITLE_MAX", "seo_issues_for", "seo_status_for"]
