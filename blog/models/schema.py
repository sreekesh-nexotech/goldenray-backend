"""Content schema: collections (public routes), templates and their image groups / attribute slots (PLAN §2.8).

The legacy CMS three-layer model is kept (CMS_BLUEPRINT §4): a **collection** registers a content type and its public
route ``content/<api_uid>/``; a **template** declares which image groups and typed attribute slots an entry exposes;
entries reference groups and slots by their stable string ``key`` (the delivery contract — ``imgUrls.<key>``,
``attributes.<key>``), never by foreign key, so relabelling is free. Keys are immutable through the API.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from blog.validators import validate_api_uid, validate_key, validate_path_prefix
from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)


class Collection(BaseModel):
    """A content type with its own public route (``articles`` → ``/api/public/v1/content/articles/``)."""

    api_uid = models.CharField(max_length=80, validators=[validate_api_uid], help_text="public route segment, e.g. 'articles'")
    singular_name = models.CharField(max_length=80)
    plural_name = models.CharField(max_length=80, help_text="PLAN §2.8 `name` (the legacy CMS display name)")
    description = models.TextField(blank=True, default="")
    path_prefix = models.CharField(max_length=120, validators=[validate_path_prefix], help_text="site path entries are published under, e.g. '/blog'")
    is_active = models.BooleanField(default=True, help_text="an inactive collection's whole route answers 404")

    class Meta:
        db_table = "blog_collection"
        ordering = ["plural_name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["api_uid"], condition=LIVE, name="blog_collection_api_uid_live_uniq"),
        ]

    def __str__(self) -> str:
        return self.plural_name


class Template(BaseModel):
    """A layout an entry is assigned to; owns image-group and attribute-slot definitions. Reusable across collections."""

    slug = models.CharField(max_length=120)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "blog_template"
        ordering = ["sort_order", "name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="blog_template_slug_live_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class TemplateImageGroup(BaseModel):
    """A named image group: single (one URL string in ``imgUrls``) or repeatable (an array, optionally capped)."""

    # CASCADE: a group is a true child of its template (templates are only soft-deleted in practice).
    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="image_groups")
    key = models.CharField(max_length=60, validators=[validate_key], help_text="stable machine name, e.g. coverImg (immutable)")
    label = models.CharField(max_length=120)
    repeatable = models.BooleanField(default=False)
    max_items = models.PositiveIntegerField(null=True, blank=True, help_text="cap for a repeatable group; null = unbounded")
    required = models.BooleanField(default=False, help_text="enforced at publish time only")
    position = models.IntegerField(default=0)

    class Meta:
        db_table = "blog_template_image_group"
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["template", "key"], condition=LIVE, name="blog_template_image_group_key_live_uniq"),
            models.CheckConstraint(condition=Q(max_items__isnull=True) | (Q(max_items__gte=1) & Q(repeatable=True)), name="blog_template_image_group_max_items_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.template_id}:{self.key}"


class TemplateAttributeSlot(BaseModel):
    """A typed attribute slot; values live in ``blog_entry_attribute_value`` keyed by ``key``."""

    class Type(models.TextChoices):
        TEXT = "TEXT", "Text"
        RICHTEXT_BLOCKS = "RICHTEXT_BLOCKS", "Rich text (blocks)"
        NUMBER = "NUMBER", "Number"
        BOOL = "BOOL", "Yes / no"
        DATE = "DATE", "Date"
        ENUM = "ENUM", "Choice"
        URL = "URL", "URL"

    # CASCADE: a slot is a true child of its template.
    template = models.ForeignKey(Template, on_delete=models.CASCADE, related_name="attribute_slots")
    key = models.CharField(max_length=60, validators=[validate_key], help_text="stable machine name (immutable)")
    label = models.CharField(max_length=120)
    type = models.CharField(max_length=16, choices=Type.choices, default=Type.TEXT)
    # Validated document: {"choices": [...]} for ENUM, {"min", "max", "default"} for NUMBER, {} otherwise.
    options = models.JSONField(default=dict, blank=True)
    required = models.BooleanField(default=False, help_text="enforced at publish time only")
    position = models.IntegerField(default=0)

    class Meta:
        db_table = "blog_template_attribute_slot"
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["template", "key"], condition=LIVE, name="blog_template_attribute_slot_key_live_uniq"),
            models.CheckConstraint(condition=Q(type__in=["TEXT", "RICHTEXT_BLOCKS", "NUMBER", "BOOL", "DATE", "ENUM", "URL"]), name="blog_template_attribute_slot_type_valid"),
        ]

    def __str__(self) -> str:
        return f"{self.template_id}:{self.key}"
