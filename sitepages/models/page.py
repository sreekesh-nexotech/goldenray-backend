"""Maintained website pages (PLAN §2.8 ``sitepages_*``; legacy CMS ``sitepages``).

A page is a real route of the Next.js site, registered by developers (``sitepages.services.registry``) — never
created or deleted from the Studio. Maintainers fill what the page declares and nothing else:

* :class:`PageTextSlot` — one exposed, length-capped string the page component reads by ``key``;
* :class:`PageImageSlot` — one replaceable image (a public media asset or an external URL) read by ``key``;
* :class:`PageSeo` — the page's SEO block (``seo.models.SeoFields``), created on the first SEO edit.

``slug`` is the public identifier (``GET /api/public/v1/pages/<slug>/``); ``route`` is the site path the legacy
``page-content?route=`` contract and FAQs are keyed on. The career page is the page with slug ``career``.
Every legacy column has a home here; the mapping is in ``docs/decisions/content-pages.md``.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel
from seo.models import SchemaType, SeoFields

LIVE = Q(deleted_at__isnull=True)
SLUG_RE = r"^[a-z0-9]+(-[a-z0-9]+)*$"
ROUTE_RE = r"^/[^\s?#]*$"
SLOT_KEY_RE = r"^[A-Za-z][A-Za-z0-9_-]*$"


class Page(BaseModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PUBLISHED = "PUBLISHED", "Published"
        ARCHIVED = "ARCHIVED", "Archived"

    slug = models.SlugField(max_length=80, help_text="public identifier, e.g. 'career'")
    route = models.CharField(max_length=255, help_text="site-relative path, e.g. '/career'")
    title = models.CharField(max_length=160, help_text="how the page is referred to internally (legacy `name`)")
    description = models.TextField(blank=True, default="", help_text="what this page is for; shown to maintainers")
    group = models.CharField(max_length=80, blank=True, default="", help_text="grouping for the Pages list, e.g. 'Solutions'")
    template = models.CharField(max_length=32, blank=True, default="", help_text="page component family on the website")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    is_protected = models.BooleanField(default=True, help_text="copy/layout is developer-owned; only slots and SEO are editable")
    sort_order = models.IntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True, help_text="content reviewed; cleared by every content change")
    # Attribution: SET_NULL so removing a user never removes the page.
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "sitepages_page"
        ordering = ["sort_order", "title", "id"]
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="sitepages_page_slug_uniq"),
            models.UniqueConstraint(fields=["route"], condition=LIVE, name="sitepages_page_route_uniq"),
            models.CheckConstraint(condition=Q(status__in=["DRAFT", "PUBLISHED", "ARCHIVED"]), name="sitepages_page_status_valid"),
            models.CheckConstraint(condition=Q(slug__regex=SLUG_RE), name="sitepages_page_slug_format"),
            models.CheckConstraint(condition=Q(route__regex=ROUTE_RE), name="sitepages_page_route_format"),
        ]
        indexes = [
            # Pages list: filter by status / group, ordered by sort_order.
            models.Index(fields=["status", "sort_order"], name="sitepages_page_status_sort"),
            models.Index(fields=["group", "sort_order"], name="sitepages_page_group_sort"),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.route})"

    @property
    def is_published(self) -> bool:
        return self.status == self.Status.PUBLISHED


class PageSeo(BaseModel, SeoFields):
    """SEO + structured data for one page (OneToOne). Split from the page so SEO tooling reads one shape."""

    # CASCADE: a true child of the page.
    page = models.OneToOneField(Page, on_delete=models.CASCADE, related_name="seo")

    class Meta:
        db_table = "sitepages_page_seo"
        constraints = [
            models.CheckConstraint(condition=Q(schema_type__in=list(SchemaType.values)), name="sitepages_page_seo_schema_type_valid"),
        ]

    def __str__(self) -> str:
        return f"SEO for {self.page.route}"

    def seo_fallback_title(self) -> str:
        return self.page.title

    def seo_path(self) -> str:
        return self.page.route


class PageTextSlot(BaseModel):
    """One explicitly exposed string. ``key`` is the contract with the page component; only ``value`` is editable."""

    class Kind(models.TextChoices):
        SHORT_TEXT = "SHORT_TEXT", "Short text"
        LONG_TEXT = "LONG_TEXT", "Long text"
        RICH_TEXT = "RICH_TEXT", "Rich text (light HTML)"
        MARKDOWN = "MARKDOWN", "Markdown"
        URL = "URL", "URL"
        EMAIL = "EMAIL", "E-mail"
        PHONE = "PHONE", "Phone"

    # CASCADE: a true child of the page.
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="text_slots")
    key = models.CharField(max_length=60, help_text="stable machine name the page component reads")
    label = models.CharField(max_length=120)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.SHORT_TEXT)
    guidance = models.CharField(max_length=255, blank=True, default="")
    value = models.TextField(blank=True, default="", help_text="empty = the page keeps its built-in text")
    max_length = models.PositiveIntegerField(null=True, blank=True, help_text="cap enforced on every edit; null = uncapped")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "sitepages_page_text_slot"
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["page", "key"], condition=LIVE, name="sitepages_text_slot_page_key_uniq"),
            models.CheckConstraint(condition=Q(kind__in=["SHORT_TEXT", "LONG_TEXT", "RICH_TEXT", "MARKDOWN", "URL", "EMAIL", "PHONE"]), name="sitepages_text_slot_kind_valid"),
            models.CheckConstraint(condition=Q(key__regex=SLOT_KEY_RE), name="sitepages_text_slot_key_format"),
            models.CheckConstraint(condition=Q(max_length__isnull=True) | Q(max_length__gt=0), name="sitepages_text_slot_max_length_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.page.route}:{self.key}"


class PageImageSlot(BaseModel):
    """One replaceable image. Empty (no asset, no URL) = the page keeps its built-in image."""

    # CASCADE: a true child of the page.
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="image_slots")
    key = models.CharField(max_length=60, help_text="stable machine name the page component reads")
    label = models.CharField(max_length=120)
    guidance = models.CharField(max_length=255, blank=True, default="", help_text="e.g. 'landscape, at least 1600×900'")
    # SET_NULL: losing the image falls back to the page's built-in one; media.usage refuses deleting a used asset.
    asset = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    external_url = models.URLField(max_length=1000, blank=True, default="", help_text="used when no asset is chosen")
    alt = models.CharField(max_length=255, blank=True, default="", help_text="overrides the asset's own alt text here")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "sitepages_page_image_slot"
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["page", "key"], condition=LIVE, name="sitepages_image_slot_page_key_uniq"),
            models.CheckConstraint(condition=Q(key__regex=SLOT_KEY_RE), name="sitepages_image_slot_key_format"),
        ]

    def __str__(self) -> str:
        return f"{self.page.route}:{self.key}"

    @property
    def effective_alt(self) -> str:
        return self.alt or (self.asset.alternative_text if self.asset_id and self.asset else "")
