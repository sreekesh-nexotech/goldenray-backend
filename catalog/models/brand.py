"""``catalog_brand`` (PLAN §2.2): one row per manufacturer. The name is unique case-insensitively among live rows."""

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)


class Brand(BaseModel):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120)
    country = models.CharField(max_length=64, blank=True, default="")
    website = models.URLField(max_length=200, blank=True, default="")
    # SET_NULL: a logo is optional; media.usage refuses to delete an asset while a live brand references it.
    logo = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "catalog_brand"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(Lower("name"), condition=LIVE, name="catalog_brand_name_live_uniq"),
            models.UniqueConstraint(fields=["slug"], condition=LIVE, name="catalog_brand_slug_live_uniq"),
            models.CheckConstraint(condition=~Q(name=""), name="catalog_brand_name_not_blank"),
        ]

    def __str__(self) -> str:
        return self.name
