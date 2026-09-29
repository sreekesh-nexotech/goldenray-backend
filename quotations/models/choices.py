"""Enumerations of the quotations tables (every enum column has a ``CheckConstraint`` built with :func:`in_choices`)."""

from django.db import models
from django.db.models import Q


class QuotationStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ISSUED = "ISSUED", "Issued"
    ACCEPTED = "ACCEPTED", "Accepted"
    EXPIRED = "EXPIRED", "Expired"
    SUPERSEDED = "SUPERSEDED", "Superseded"
    CANCELLED = "CANCELLED", "Cancelled"


class VersionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ISSUED = "ISSUED", "Issued"
    SUPERSEDED = "SUPERSEDED", "Superseded"


class QuotationSource(models.TextChoices):
    """Flarize ``quotationSource`` (classification of who brought the customer)."""

    DIRECT = "DIRECT", "Direct"
    AFFILIATE = "AFFILIATE", "Affiliate"
    DISTRICT = "DISTRICT", "District"


class SystemType(models.TextChoices):
    ONGRID = "ONGRID", "On-grid"
    HYBRID = "HYBRID", "Hybrid"


class Tier(models.TextChoices):
    BASE = "BASE", "Base"
    VALUE = "VALUE", "Value"
    PREMIUM = "PREMIUM", "Premium"


class Phase(models.TextChoices):
    ONE = "1P", "Single phase"
    THREE = "3P", "Three phase"


class RoofType(models.TextChoices):
    """Installation / roof type of the pack's structure template (Flarize ``installationType``)."""

    FLAT = "FLAT", "Flat roof"
    SHEET = "SHEET", "Sheet roof"
    ELEVATED = "ELEVATED", "Elevated structure"


class SubsidyType(models.TextChoices):
    RESIDENTIAL = "residential", "Residential (PM Surya Ghar)"
    GHS = "ghs", "Group housing society"
    NONE = "none", "No subsidy"


class Language(models.TextChoices):
    EN = "en", "English"
    ML = "ml", "Malayalam"


class DiscountStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class ContentStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PUBLISHED = "PUBLISHED", "Published"
    SUPERSEDED = "SUPERSEDED", "Superseded"


class InclusionKind(models.TextChoices):
    """COMPONENT: a per-tier included/not included item; SERVICE: a row of the page-5 "What's Included" table."""

    COMPONENT = "COMPONENT", "Component inclusion"
    SERVICE = "SERVICE", "Service matrix row"


class EmailChannel(models.TextChoices):
    EMAIL = "EMAIL", "E-mail"
    LEGACY_LINK = "LEGACY_LINK", "Legacy website quote link"


class EmailStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"


TIERS = ("BASE", "VALUE", "PREMIUM")


def in_choices(field: str, choices) -> Q:
    return Q(**{f"{field}__in": list(choices.values)})
