"""Explicit attribution stamping (no ``hasattr`` magic: every stamped model is a ``BaseModel``)."""

from __future__ import annotations

from django.utils import timezone

from core.models.base import BaseModel, actor_or_none


def _require_base(instance) -> None:
    if not isinstance(instance, BaseModel):
        raise TypeError(f"{instance.__class__.__name__} is not a core.models.BaseModel; stamp its attribution columns explicitly.")


def stamp_create(instance: BaseModel, user) -> BaseModel:
    """Set ``created_by``/``updated_by`` on a new instance (before its first save)."""
    _require_base(instance)
    actor = actor_or_none(user)
    instance.created_by = actor
    instance.updated_by = actor
    return instance


def stamp_update(instance: BaseModel, user) -> BaseModel:
    _require_base(instance)
    instance.updated_by = actor_or_none(user)
    instance.updated_at = timezone.now()
    return instance


def create_stamped(model: type[BaseModel], *, user, **values) -> BaseModel:
    """``model.objects.create(**values)`` with attribution stamped."""
    instance = model(**values)
    stamp_create(instance, user)
    instance.save()
    return instance
