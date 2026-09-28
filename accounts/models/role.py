from django.db import models
from django.db.models import Q

from accounts.registry import normalise_permissions, normalise_scopes
from core.models import BaseModel


class Role(BaseModel):
    """A named grant set. ``permissions``/``scopes`` are normalised against ``accounts.registry`` on every write.

    Services validate strictly (``normalise_permissions(..., strict=True)``) and report unknown entries; the model
    normalises leniently as a last line of defence, so no code path can persist a grant the registry lacks.
    """

    class LegacyRole(models.TextChoices):
        ADMIN = "admin", "CMS admin"
        EDITOR = "editor", "CMS editor"
        AUTHOR = "author", "CMS author"

    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    is_system = models.BooleanField(default=False)
    permissions = models.JSONField(default=dict, blank=True)
    scopes = models.JSONField(default=dict, blank=True)
    legacy_role = models.CharField(max_length=16, null=True, blank=True, choices=LegacyRole.choices)

    class Meta:
        db_table = "accounts_role"
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=Q(deleted_at__isnull=True), name="accounts_role_slug_uniq"),
            models.CheckConstraint(condition=Q(legacy_role__isnull=True) | Q(legacy_role__in=["admin", "editor", "author"]), name="accounts_role_legacy_role_valid"),
        ]

    def __str__(self):
        return self.name

    def normalise_grants(self) -> None:
        self.permissions = normalise_permissions(self.permissions)
        self.scopes = normalise_scopes(self.scopes, self.permissions)

    def save(self, *args, **kwargs):
        self.normalise_grants()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and ({"permissions", "scopes"} & set(update_fields)):
            kwargs["update_fields"] = list(dict.fromkeys([*update_fields, "permissions", "scopes"]))
        super().save(*args, **kwargs)

    def versioned_update(self, user=None, **values) -> None:
        if "permissions" in values or "scopes" in values:
            permissions = normalise_permissions(values.get("permissions", self.permissions))
            values["permissions"] = permissions
            values["scopes"] = normalise_scopes(values.get("scopes", self.scopes), permissions)
        super().versioned_update(user, **values)
