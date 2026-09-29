"""FAQs (PLAN §2.8 ``faqs_*``; legacy CMS ``faqs``).

One record per question, bound to the maintained page (``sitepages.Page``) it appears on, grouped by a free-text
``section`` inside the page, ordered by ``sort_order`` within ``(page, section)``, optionally labelled with a
category. ``status`` moves only through the workflow services (publish / unpublish / archive / restore); the FAQPage
JSON-LD is generated from the published records (``seo.schema.faq_page``), never typed.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel
from seo.models import SchemaType, SeoFields

LIVE = Q(deleted_at__isnull=True)


class FaqCategory(BaseModel):
    """Optional grouping label for FAQs (legacy ``faqs_category``)."""

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, help_text="inactive categories stay on their FAQs but are not offered")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "faqs_category"
        ordering = ["sort_order", "name", "id"]
        verbose_name_plural = "FAQ categories"
        constraints = [
            models.UniqueConstraint(fields=["name"], condition=LIVE, name="faqs_category_name_uniq"),
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="faqs_category_slug_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class Faq(BaseModel, SeoFields):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PUBLISHED = "PUBLISHED", "Published"
        ARCHIVED = "ARCHIVED", "Archived"

    question = models.CharField(max_length=500)
    # Blank is allowed on purpose: a draft may be incomplete; publishing is what requires an answer.
    answer = models.TextField(blank=True, default="", help_text="plain text or light HTML; the FAQPage schema carries it as is")
    # SET_NULL: pages are only soft-deleted, so this never fires in practice; a FAQ without a page cannot be published.
    page = models.ForeignKey("sitepages.Page", null=True, blank=True, on_delete=models.SET_NULL, related_name="faqs")
    section = models.CharField(max_length=80, blank=True, default="", help_text="block within the page when it carries several FAQ lists")
    # PROTECT: a lookup; the category service refuses deleting a category that live FAQs use.
    category = models.ForeignKey(FaqCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="faqs")
    sort_order = models.IntegerField(default=0, help_text="order within the page/section (legacy display_order)")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True, help_text="first publication; never cleared")
    archived_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True, help_text="content reviewed; cleared by every content change")
    # Attribution: SET_NULL so removing a user never removes the FAQ.
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "faqs_faq"
        # Page → section → author-set order is the order the site renders in.
        ordering = ["page_id", "section", "sort_order", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["DRAFT", "PUBLISHED", "ARCHIVED"]), name="faqs_faq_status_valid"),
            models.CheckConstraint(condition=Q(schema_type__in=list(SchemaType.values)), name="faqs_faq_schema_type_valid"),
        ]
        indexes = [
            # Public delivery and reorder: one page's FAQs in display order.
            models.Index(fields=["page", "section", "sort_order"], name="faqs_faq_page_section_sort"),
            # Studio list: filter by status, newest edits first.
            models.Index(fields=["status", "updated_at"], name="faqs_faq_status_updated"),
        ]

    def __str__(self) -> str:
        return self.question[:80]

    def seo_fallback_title(self) -> str:
        return self.question

    def seo_path(self) -> str:
        return self.page.route if self.page_id and self.page else ""
