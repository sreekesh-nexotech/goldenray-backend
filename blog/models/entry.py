"""Entries and their children (PLAN §2.8 ``blog_entry`` … ``blog_entry_seo``).

Every legacy ``content_entry`` column has a home here: ``summary``/``introduction`` (Strapi-blocks arrays),
``warning``/``insights`` (callouts), ``published_on`` (author-set display date, a timestamp as in the CMS — DV-34),
``published_at`` (system first-publish stamp, set once), ``archived_at`` (the CMS ``deleted`` state's stamp). The CMS
``document_id`` is imported into ``uid`` unchanged (the frontend's ``documentId``).

Lifecycle: ``DRAFT → REVIEW → PUBLISHED → ARCHIVED`` through POST actions only (``blog.services.workflow``); a
scheduled entry keeps its status and carries ``scheduled_for`` until the Beat task publishes it. Soft delete
(``deleted_at``) is separate from the lifecycle and frees the slug.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from blog.models.schema import Collection, Template
from blog.models.taxonomy import Author, Badge, Category, Tag
from blog.validators import validate_entry_slug
from core.models import BaseModel
from seo.models import SeoFields

LIVE = Q(deleted_at__isnull=True)


class Entry(BaseModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        REVIEW = "REVIEW", "In review"
        PUBLISHED = "PUBLISHED", "Published"
        ARCHIVED = "ARCHIVED", "Archived"

    delivery_id = models.BigIntegerField(editable=False, help_text="the Strapi numeric `id` of the delivery contract (DV-34)")
    # PROTECT: a collection is a master; deleting it out from under content must fail.
    collection = models.ForeignKey(Collection, on_delete=models.PROTECT, related_name="entries")
    # PROTECT: a template is a master (publish validation and imgUrls cardinality depend on it; DV-34).
    template = models.ForeignKey(Template, null=True, blank=True, on_delete=models.PROTECT, related_name="entries")

    title = models.CharField(max_length=255)
    slug = models.CharField(max_length=255, validators=[validate_entry_slug])
    excerpt = models.TextField(blank=True, default="")
    summary = models.JSONField(default=list, blank=True, help_text="Strapi-blocks array")
    introduction = models.JSONField(default=list, blank=True, help_text="Strapi-blocks array")
    warning = models.TextField(blank=True, default="", help_text="'important' callout")
    insights = models.TextField(blank=True, default="", help_text="'key insight' callout")
    read_time = models.PositiveSmallIntegerField(null=True, blank=True, help_text="minutes")
    is_featured = models.BooleanField(default=False)
    sort_order = models.IntegerField(null=True, blank=True)
    locale = models.CharField(max_length=2, default="en")

    # SET_NULL: the byline is optional; authors are only soft-deleted and refused while in use.
    author = models.ForeignKey(Author, null=True, blank=True, on_delete=models.SET_NULL, related_name="entries")
    # SET_NULL: media.usage refuses deleting an asset a live entry references.
    cover_image = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    categories = models.ManyToManyField(Category, through="EntryCategory", related_name="entries", blank=True)
    tags = models.ManyToManyField(Tag, through="EntryTag", related_name="entries", blank=True)
    badges = models.ManyToManyField(Badge, through="EntryBadge", related_name="entries", blank=True)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True, help_text="system first-publish stamp (never refreshed)")
    published_on = models.DateTimeField(null=True, blank=True, help_text="author-set display date (seeded from published_at)")
    scheduled_for = models.DateTimeField(null=True, blank=True, help_text="the Beat task publishes the entry at this time")
    archived_at = models.DateTimeField(null=True, blank=True)
    # SET_NULL: attribution.
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "blog_entry"
        # The delivery default order (legacy CMS ordering); delivery_id makes ties deterministic.
        ordering = ["sort_order", "-published_on", "-created_at", "delivery_id"]
        constraints = [
            models.UniqueConstraint(fields=["collection", "slug"], condition=LIVE, name="blog_entry_collection_slug_live_uniq"),
            models.UniqueConstraint(fields=["delivery_id"], name="blog_entry_delivery_id_uniq"),
            models.CheckConstraint(condition=Q(status__in=["DRAFT", "REVIEW", "PUBLISHED", "ARCHIVED"]), name="blog_entry_status_valid"),
            models.CheckConstraint(condition=~Q(status="PUBLISHED") | Q(published_at__isnull=False), name="blog_entry_published_has_published_at"),
            models.CheckConstraint(condition=~Q(status="ARCHIVED") | Q(archived_at__isnull=False), name="blog_entry_archived_has_archived_at"),
            models.CheckConstraint(condition=Q(scheduled_for__isnull=True) | Q(status__in=["DRAFT", "REVIEW"]), name="blog_entry_schedule_only_unpublished"),
            models.CheckConstraint(condition=Q(verified_by__isnull=True) | Q(verified_at__isnull=False), name="blog_entry_verified_has_verified_at"),
            models.CheckConstraint(condition=Q(locale__regex=r"^[a-z]{2}$"), name="blog_entry_locale_format"),
        ]
        indexes = [
            # Delivery list: published entries of one collection in display order.
            models.Index(fields=["collection", "status", "published_on"], name="blog_entry_delivery_idx"),
            # Staff list: status filter, newest first.
            models.Index(fields=["status", "updated_at"], name="blog_entry_status_updated_idx"),
            # Beat: due scheduled entries.
            models.Index(fields=["scheduled_for"], condition=Q(scheduled_for__isnull=False), name="blog_entry_scheduled_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.title} [{self.status}]"

    @property
    def path(self) -> str:
        return f"{self.collection.path_prefix}/{self.slug}"


class EntryCategory(models.Model):
    """Entry ↔ category link (no base: link rows are replaced, not versioned — DV-34)."""

    id = models.BigAutoField(primary_key=True)
    # CASCADE: the link is a child of the entry.
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="+")
    # PROTECT: categories are lookups (refused while in use).
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "blog_entry_category"
        constraints = [models.UniqueConstraint(fields=["entry", "category"], name="blog_entry_category_uniq")]


class EntryTag(models.Model):
    id = models.BigAutoField(primary_key=True)
    # CASCADE: the link is a child of the entry.
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="+")
    # PROTECT: tags are lookups.
    tag = models.ForeignKey(Tag, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "blog_entry_tag"
        constraints = [models.UniqueConstraint(fields=["entry", "tag"], name="blog_entry_tag_uniq")]


class EntryBadge(models.Model):
    id = models.BigAutoField(primary_key=True)
    # CASCADE: the link is a child of the entry.
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="+")
    # PROTECT: badges are lookups.
    badge = models.ForeignKey(Badge, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "blog_entry_badge"
        constraints = [models.UniqueConstraint(fields=["entry", "badge"], name="blog_entry_badge_uniq")]


class EntrySlugHistory(BaseModel):
    """A slug an entry used to be published under; active aliases resolve to the entry (only while it is published).

    ``collection`` is denormalised because it is the uniqueness scope (entry slugs are unique per collection, so
    aliases must be too). ``active=False`` retires an alias without losing the audit trail.
    """

    # CASCADE: history is a child of the entry.
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="slug_history")
    # PROTECT: the uniqueness scope; collections are masters.
    collection = models.ForeignKey(Collection, on_delete=models.PROTECT, related_name="+")
    slug = models.CharField(max_length=255)
    active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "blog_entry_slug_history"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["collection", "slug"], condition=LIVE & Q(active=True), name="blog_entry_slug_history_active_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.slug} → entry #{self.entry_id} ({'active' if self.active else 'retired'})"


class ContentBlock(BaseModel):
    """One ordered body section (``contentBlocks[]``). ``component`` is the delivered ``__component`` uid."""

    class Kind(models.TextChoices):
        RICH_TEXT = "RICH_TEXT", "Rich text"
        IMAGE = "IMAGE", "Image"
        QUOTE = "QUOTE", "Quote"
        CTA = "CTA", "Call to action"
        EMBED = "EMBED", "Embed"
        TABLE = "TABLE", "Table"

    delivery_id = models.BigIntegerField(editable=False)
    # CASCADE: a block is a true child of its entry.
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="content_blocks")
    position = models.IntegerField(default=0)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.RICH_TEXT)
    component = models.CharField(max_length=120, default="shared.rich-text")
    data = models.JSONField(default=list, blank=True, help_text="the block body (Strapi-blocks array for RICH_TEXT)")

    class Meta:
        db_table = "blog_content_block"
        ordering = ["position", "delivery_id"]
        constraints = [
            models.UniqueConstraint(fields=["delivery_id"], name="blog_content_block_delivery_id_uniq"),
            models.CheckConstraint(condition=Q(kind__in=["RICH_TEXT", "IMAGE", "QUOTE", "CTA", "EMBED", "TABLE"]), name="blog_content_block_kind_valid"),
        ]


class EntryImage(BaseModel):
    """An image bound to a template image group: a media asset **or** an external URL (exactly one)."""

    # CASCADE: an image row is a true child of its entry.
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="images")
    group_key = models.CharField(max_length=60)
    position = models.IntegerField(default=0)
    # SET_NULL: media.usage refuses deleting an asset a live image row references.
    media_asset = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    external_url = models.URLField(max_length=1000, blank=True, default="")
    alt = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "blog_entry_image"
        ordering = ["group_key", "position", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(media_asset__isnull=False) & Q(external_url="")) | (Q(media_asset__isnull=True) & ~Q(external_url="")),
                name="blog_entry_image_one_source",
            ),
        ]


class EntryAttributeValue(BaseModel):
    """Typed value for a template attribute slot (validated against the slot's type on every write)."""

    # CASCADE: a value is a true child of its entry.
    entry = models.ForeignKey(Entry, on_delete=models.CASCADE, related_name="attribute_values")
    slot_key = models.CharField(max_length=60)
    value = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "blog_entry_attribute_value"
        ordering = ["slot_key", "id"]
        constraints = [
            models.UniqueConstraint(fields=["entry", "slot_key"], condition=LIVE, name="blog_entry_attribute_value_slot_live_uniq"),
        ]


class EntrySeo(SeoFields, BaseModel):
    """The entry's SEO block (shared ``SeoFields`` + the legacy ``keywords``)."""

    # CASCADE: the SEO block is a true child of its entry (one row per entry, restored rather than re-created).
    entry = models.OneToOneField(Entry, on_delete=models.CASCADE, related_name="seo")
    keywords = models.TextField(blank=True, default="", help_text="comma-separated meta keywords (legacy CMS)")

    class Meta:
        db_table = "blog_entry_seo"

    def seo_fallback_title(self) -> str:
        return self.entry.title

    def seo_path(self) -> str:
        return self.entry.path
