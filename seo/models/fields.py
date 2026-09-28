"""Shared SEO + schema fields (ported from the legacy CMS seo app; PLAN §2.8 seo_fields, DV-15).

Phase 1 SEO is deliberately small: a title, a description, a canonical URL, and
enough structured-data control to emit correct JSON-LD. Three modules need
exactly that same set — Pages (§6.2), FAQs (§6.5) and Job Positions (§6.12) —
so it lives here as an abstract mixin rather than being retyped three times and
drifting apart.

The scope is explicit that raw JSON must not be put in front of normal content
users, and that FAQPage schema is *generated from the structured record* rather
than typed. So ``schema_type`` selects the shape, ``schema_extra`` holds only
the handful of fields the generator cannot infer, and each module supplies its
own builder in ``schema.py``. Nobody hand-writes JSON-LD.
"""

from django.db import models


class SchemaType(models.TextChoices):
    """The structured-data shapes Phase 1 supports.

    Deliberately short. §6.7 asks for "schema type and basic structured fields",
    FAQPage for FAQ records and Article where blogs already support it — not a
    general schema.org editor.
    """

    NONE = "none", "No structured data"
    WEB_PAGE = "WebPage", "Web page"
    ARTICLE = "Article", "Article"
    FAQ_PAGE = "FAQPage", "FAQ page"
    JOB_POSTING = "JobPosting", "Job posting"
    ORGANIZATION = "Organization", "Organization"
    BREADCRUMB = "BreadcrumbList", "Breadcrumb list"


#: Soft limits used for the validity indicator (§6.7 — "simple validity/status
#: indicator; no advanced SEO scoring platform"). Google truncates around these
#: lengths; going over is a warning, never a hard validation failure, because a
#: deliberate long title is the author's call to make.
TITLE_MAX = 60
DESCRIPTION_MIN = 70
DESCRIPTION_MAX = 160


class SeoFields(models.Model):
    """Abstract SEO block mixed into every publicly addressable record."""

    seo_title = models.CharField(max_length=255, blank=True, help_text=f"falls back to the record's own title; ~{TITLE_MAX} chars")
    meta_description = models.TextField(blank=True, help_text=f"{DESCRIPTION_MIN}–{DESCRIPTION_MAX} characters reads best in results")
    canonical_url = models.URLField(max_length=1000, blank=True, help_text="only when this URL duplicates another")
    og_title = models.CharField(max_length=255, blank=True, help_text="social share title; falls back to the SEO title")
    og_description = models.TextField(blank=True, help_text="social share description; falls back to the meta description")
    og_image = models.ForeignKey(
        "media.MediaAsset",
        on_delete=models.SET_NULL,  # attribution-style reference: losing the image must not delete the record
        null=True,
        blank=True,
        related_name="+",
        help_text="social share image",
    )
    schema_type = models.CharField(max_length=32, choices=SchemaType.choices, default=SchemaType.NONE)
    schema_extra = models.JSONField(
        default=dict,
        blank=True,
        help_text="only the structured fields the generator cannot infer from the record",
    )
    noindex = models.BooleanField(default=False, help_text="ask search engines not to index this URL")

    class Meta:
        abstract = True

    # ── Validity indicator ───────────────────────────────────────────────────
    def seo_issues(self) -> list[dict]:
        """Plain-language problems with this record's SEO, worst first.

        Returns dicts of ``{level, field, message}``. ``level`` is "error" for
        something that will actually misbehave (a missing title has no fallback,
        an indexed page with no description gets one invented for it) and
        "warning" for length advice. The Studio renders these directly, so the
        messages are written for a content manager rather than a developer.
        """
        issues: list[dict] = []
        title = (self.seo_title or self.seo_fallback_title() or "").strip()
        desc = (self.meta_description or "").strip()

        if not title:
            issues.append({"level": "error", "field": "seo_title", "message": "No SEO title, and nothing to fall back on."})
        elif len(title) > TITLE_MAX:
            issues.append(
                {
                    "level": "warning",
                    "field": "seo_title",
                    "message": f"Title is {len(title)} characters — search results usually cut off near {TITLE_MAX}.",
                }
            )

        if not desc:
            issues.append(
                {
                    "level": "error" if not self.noindex else "warning",
                    "field": "meta_description",
                    "message": "No meta description — search engines will invent one from the page copy.",
                }
            )
        elif len(desc) > DESCRIPTION_MAX:
            issues.append(
                {
                    "level": "warning",
                    "field": "meta_description",
                    "message": f"Description is {len(desc)} characters — usually cut off near {DESCRIPTION_MAX}.",
                }
            )
        elif len(desc) < DESCRIPTION_MIN:
            issues.append(
                {
                    "level": "warning",
                    "field": "meta_description",
                    "message": f"Description is only {len(desc)} characters — under {DESCRIPTION_MIN} tends to read as thin.",
                }
            )

        return issues

    def seo_status(self) -> str:
        """``ok`` | ``warning`` | ``error`` — the one-glance indicator."""
        levels = {i["level"] for i in self.seo_issues()}
        if "error" in levels:
            return "error"
        if "warning" in levels:
            return "warning"
        return "ok"

    # ── Hooks for the concrete model ─────────────────────────────────────────
    def seo_fallback_title(self) -> str:
        """The record's own title, used when no SEO title is set. Override."""
        return getattr(self, "title", "") or getattr(self, "name", "") or ""

    def seo_path(self) -> str:
        """Site-relative path this record is published at. Override."""
        return getattr(self, "route", "") or ""
