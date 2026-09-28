"""``BaseModel``: the columns every business table carries.

* ``id`` BigAutoField — physical key, never exposed outside the service layer;
* ``uid`` UUID — the external identifier used in every path and body;
* ``created_at`` / ``updated_at``; ``created_by`` / ``updated_by`` (SET_NULL attribution, stamped explicitly by
  services — see ``core.services.stamping``);
* ``deleted_at`` — the single soft-delete mechanism; ``objects`` returns live rows, ``all_objects`` everything;
* ``version`` — optimistic locking; every versioned write is a compare-and-swap on it.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F
from django.utils import timezone

from core.errors import StaleVersion


class BaseQuerySet(models.QuerySet):
    def live(self):
        return self.filter(deleted_at__isnull=True)

    def deleted(self):
        return self.filter(deleted_at__isnull=False)


class LiveManager(models.Manager.from_queryset(BaseQuerySet)):
    """Default manager: live (not soft-deleted) rows only."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class AllObjectsManager(models.Manager.from_queryset(BaseQuerySet)):
    """Every row including soft-deleted ones (imports, restores, audits)."""


def actor_or_none(user):
    """The ``accounts.User`` to stamp, or ``None`` for anonymous/system/service callers."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    from django.contrib.auth import get_user_model

    return user if isinstance(user, get_user_model()) and user.pk is not None else None


class BaseModel(models.Model):
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(default=timezone.now, editable=False)
    # Attribution: SET_NULL so deleting a user never deletes business data.
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False)
    # Attribution: SET_NULL, see created_by.
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False)
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)
    version = models.PositiveIntegerField(default=1, editable=False)

    objects = LiveManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.updated_at = timezone.now()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "updated_at" not in update_fields:
            kwargs["update_fields"] = [*update_fields, "updated_at"]
        super().save(*args, **kwargs)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def versioned_update(self, user=None, **values) -> None:
        """Compare-and-swap UPDATE of ``values``: succeeds only if the row still has ``self.version``.

        Increments ``version``, stamps ``updated_by``/``updated_at`` and refreshes the instance attributes.
        Raises ``StaleVersion`` when another writer got there first (or the row no longer exists).
        """
        if self.pk is None:
            raise ValueError("versioned_update() needs a saved instance.")
        now = timezone.now()
        actor = actor_or_none(user)
        columns = {}
        for name, value in values.items():
            field = self._meta.get_field(name)
            columns[field.attname] = value.pk if isinstance(field, models.ForeignKey) and isinstance(value, models.Model) else value
        columns.update(updated_at=now, updated_by_id=actor.pk if actor else None, version=F("version") + 1)
        updated = type(self).all_objects.filter(pk=self.pk, version=self.version).update(**columns)
        if updated != 1:
            raise StaleVersion()
        for name, value in values.items():
            setattr(self, name, value)
        self.updated_at = now
        self.updated_by = actor
        self.version += 1

    def soft_delete(self, user=None) -> None:
        """Mark the row deleted (idempotent). Lifecycle ``status`` fields are separate from deletion."""
        if self.deleted_at is not None:
            return
        self.versioned_update(user, deleted_at=timezone.now())

    def restore(self, user=None) -> None:
        if self.deleted_at is None:
            return
        self.versioned_update(user, deleted_at=None)
