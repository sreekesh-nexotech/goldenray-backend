"""Named lookups an entry is filed under: authors, categories, tags and badges (PLAN §2.8).

Each carries ``delivery_id`` — the numeric ``id`` the Strapi-v5-flat delivery contract exposes for embedded objects
(``author.id``, ``categories[].id`` …). It is a public number, never the table's primary key: taken from a
``core.sequences`` counter on create and preserved from the CMS primary key on import (DV-34).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from blog.validators import validate_color
from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)


class Author(BaseModel):
    delivery_id = models.BigIntegerField(editable=False)
    name = models.CharField(max_length=160)
    slug = models.CharField(max_length=160)
    role = models.CharField(max_length=120, blank=True, default="", help_text="byline role, e.g. 'Solar Engineer'")
    bio = models.TextField(blank=True, default="")
    # SET_NULL: an avatar is optional; media.usage refuses deleting an asset a live author references.
    avatar = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # SET_NULL: attribution-style link to the staff account writing as this author.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "blog_author"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="blog_author_slug_live_uniq"),
            models.UniqueConstraint(fields=["delivery_id"], name="blog_author_delivery_id_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class Category(BaseModel):
    delivery_id = models.BigIntegerField(editable=False)
    name = models.CharField(max_length=120)
    # NULL only for a legacy CMS category imported without a slug (the CMS column is nullable and its delivery shows
    # ``"slug": null``; PLAN §7.2 row 4 "slugs preserved"). The staff API always sets one (generated when blank).
    slug = models.CharField(max_length=120, null=True, blank=True)

    class Meta:
        db_table = "blog_category"
        ordering = ["name", "id"]
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="blog_category_slug_live_uniq"),
            models.UniqueConstraint(fields=["delivery_id"], name="blog_category_delivery_id_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class Tag(BaseModel):
    delivery_id = models.BigIntegerField(editable=False)
    name = models.CharField(max_length=80)
    slug = models.CharField(max_length=80)

    class Meta:
        db_table = "blog_tag"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["name"], condition=LIVE, name="blog_tag_name_live_uniq"),
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="blog_tag_slug_live_uniq"),
            models.UniqueConstraint(fields=["delivery_id"], name="blog_tag_delivery_id_uniq"),
        ]

    def __str__(self) -> str:
        return self.name


class Badge(BaseModel):
    delivery_id = models.BigIntegerField(editable=False)
    name = models.CharField(max_length=120, help_text="delivered as `label` (legacy name)")
    slug = models.CharField(max_length=120)
    color = models.CharField(max_length=9, default="#123532", validators=[validate_color])

    class Meta:
        db_table = "blog_badge"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="blog_badge_slug_live_uniq"),
            models.UniqueConstraint(fields=["delivery_id"], name="blog_badge_delivery_id_uniq"),
            models.CheckConstraint(condition=Q(color__regex=r"^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$"), name="blog_badge_color_hex"),
        ]

    def __str__(self) -> str:
        return self.name
