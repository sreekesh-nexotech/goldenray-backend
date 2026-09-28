"""Registry of the columns that reference ``media_asset`` — the delete guard (PLAN §2.1, §3.4 Media).

Every app whose model points at a media asset registers the field once, from its ``AppConfig.ready()``::

    from media import usage
    usage.register(CompanyProfile, "logo")          # ForeignKey / OneToOneField to MediaAsset
    usage.register(Observation, "photos")           # or a ManyToManyField to MediaAsset

``DELETE media/<uid>/`` is refused (409 ``media_in_use``) while any **live** row references the asset. References
held only by soft-deleted rows are reported but do not block (those rows are invisible everywhere; the asset row
itself is only soft-deleted, so the database reference never dangles).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models


@dataclass(frozen=True)
class Reference:
    label: str  # "company.companyprofile.logo"
    live: int
    deleted: int


_FIELDS: dict[str, tuple[type[models.Model], str]] = {}


def _label(model: type[models.Model], field_name: str) -> str:
    return f"{model._meta.app_label}.{model._meta.model_name}.{field_name}"


def register(model: type[models.Model], field_name: str) -> None:
    """Register ``model.field_name`` (FK, one-to-one or many-to-many to ``MediaAsset``). Idempotent."""
    from media.models import MediaAsset

    try:
        field = model._meta.get_field(field_name)
    except FieldDoesNotExist as exc:
        raise ImproperlyConfigured(f"{model.__name__} has no field {field_name!r}.") from exc
    if not field.is_relation or field.related_model is not MediaAsset or not (field.many_to_one or field.one_to_one or field.many_to_many):
        raise ImproperlyConfigured(f"{model.__name__}.{field_name} is not a relation to MediaAsset.")
    _FIELDS[_label(model, field_name)] = (model, field_name)


def unregister(model: type[models.Model], field_name: str) -> None:
    _FIELDS.pop(_label(model, field_name), None)


def registered() -> dict[str, tuple[type[models.Model], str]]:
    return dict(_FIELDS)


def references(asset) -> list[Reference]:
    """Every registered column referencing ``asset``, with live/soft-deleted row counts (zero rows omitted)."""
    found: list[Reference] = []
    for label, (model, field_name) in sorted(_FIELDS.items()):
        rows = model._base_manager.filter(**{field_name: asset})
        if any(field.name == "deleted_at" for field in model._meta.concrete_fields):
            live = rows.filter(deleted_at__isnull=True).count()
            deleted = rows.filter(deleted_at__isnull=False).count()
        else:
            live, deleted = rows.count(), 0
        if live or deleted:
            found.append(Reference(label=label, live=live, deleted=deleted))
    return found
