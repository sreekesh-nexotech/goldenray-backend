"""Enumerations of the agreements tables (every enum column has a ``CheckConstraint`` built with :func:`in_choices`)."""

from django.db import models
from django.db.models import Q


class AgreementKind(models.TextChoices):
    """The Purchase Agreement page's three forms (``FORMS`` 1/2/3)."""

    PURCHASE_AGREEMENT = "PURCHASE_AGREEMENT", "Purchase agreement"
    SALE_ORDER = "SALE_ORDER", "Sale order"
    EXTRA_STRUCTURE = "EXTRA_STRUCTURE", "Extra structure agreement"


class AgreementStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ISSUED = "ISSUED", "Issued"
    ACCEPTED = "ACCEPTED", "Accepted"
    SUPERSEDED = "SUPERSEDED", "Superseded"
    CANCELLED = "CANCELLED", "Cancelled"


class Language(models.TextChoices):
    EN = "en", "English"
    ML = "ml", "Malayalam"
    HI = "hi", "Hindi"


class SystemType(models.TextChoices):
    """The site-inspection vocabulary (``agreements.issued`` carries it as is)."""

    ON_GRID = "ON_GRID", "On-grid"
    HYBRID = "HYBRID", "Hybrid"


class Phase(models.TextChoices):
    ONE = "1P", "Single phase"
    THREE = "3P", "Three phase"


class Variant(models.TextChoices):
    """The package tier printed after the price (legacy ``variant`` Base / Value / Premium)."""

    BASE = "BASE", "Base"
    VALUE = "VALUE", "Value"
    PREMIUM = "PREMIUM", "Premium"


class InverterType(models.TextChoices):
    """Legacy ``invtype`` (String Inverter / Micro Inverter / Hybrid Inverter)."""

    STRING = "STRING", "String Inverter"
    MICRO = "MICRO", "Micro Inverter"
    HYBRID = "HYBRID", "Hybrid Inverter"


class SourceType(models.TextChoices):
    """What a blank agreement was raised from (DV-3: a reference, never a foreign key into site_inspections)."""

    SITE_INSPECTION = "SITE_INSPECTION", "Site inspection"


class AcceptedVia(models.TextChoices):
    OTP = "OTP", "Customer OTP"
    PAPER = "PAPER", "Paper-signed copy"


def in_choices(field: str, choices, *, blank: bool = False) -> Q:
    values = list(choices.values) + ([""] if blank else [])
    return Q(**{f"{field}__in": values})
