"""Enums of the bom tables (``TextChoices`` + a ``CheckConstraint`` on every column that uses one)."""

from django.db import models
from django.db.models import Q

from pricing.models.choices import SystemType

LIVE = Q(deleted_at__isnull=True)

__all__ = ["LIVE", "FilterPhase", "FilterType", "ProfileInverterType", "ProfileStructureMaterial", "StructureItemType", "SystemType", "in_choices"]


def in_choices(field: str, choices: type[models.TextChoices]) -> Q:
    return Q(**{f"{field}__in": choices.values})


class FilterType(models.TextChoices):
    """Which inverters a slot accepts (legacy ``BomSlot.filter_type``)."""

    ANY = "", "Any"
    ONGRID = "ONGRID", "On-grid only (or untyped)"
    HYBRID = "HYBRID", "Hybrid only"


class FilterPhase(models.TextChoices):
    """Legacy ``BomSlot.filter_phase``: HYB = only the hybrid phase codes (``1P-HYB`` / ``3P-HYB``)."""

    ANY = "", "Phase of the system"
    HYB = "HYB", "Hybrid phase codes only"


class StructureItemType(models.TextChoices):
    TUBE = "TUBE", "Tube (priced by weight)"
    FIXED = "FIXED", "Fixed item (priced per unit)"


class ProfileStructureMaterial(models.TextChoices):
    GP = "GP", "GP (galvanised plain)"
    GI = "GI", "GI (galvanised iron)"
    AL = "AL", "Aluminium"


class ProfileInverterType(models.TextChoices):
    ONGRID = "ONGRID", "On-grid"
    HYBRID = "HYBRID", "Hybrid"
    MICRO = "MICRO", "Micro-inverter"
