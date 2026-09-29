"""Enums shared by the pricing tables (``TextChoices`` + a ``CheckConstraint`` on every column that uses one)."""

from django.db import models
from django.db.models import Q

LIVE = Q(deleted_at__isnull=True)


class PriceKind(models.TextChoices):
    PURCHASE = "PURCHASE", "Purchase price"
    LANDED = "LANDED", "Landed cost"
    LIST = "LIST", "List price"


class PriceSource(models.TextChoices):
    BATCH = "BATCH", "Procurement batch"
    MANUAL = "MANUAL", "Manual entry"
    IMPORT = "IMPORT", "Legacy import"
    MARKUP = "MARKUP", "Markup (never for LIST)"


class SystemType(models.TextChoices):
    ONGRID = "ONGRID", "On-grid"
    HYBRID = "HYBRID", "Hybrid"
    UPGRADE = "UPGRADE", "Upgrade"


class Tier(models.TextChoices):
    BASE = "BASE", "Base"
    VALUE = "VALUE", "Value"
    PREMIUM = "PREMIUM", "Premium"


class MarketRateSetStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    RETIRED = "RETIRED", "Retired"


class BatteryConfig(models.TextChoices):
    NONE = "", "Not a hybrid band"
    ZERO = "0", "0 batteries"
    ONE = "1", "1 battery"
    TWO = "2", "2 batteries"


class Phase(models.TextChoices):
    ANY = "", "Any phase"
    SINGLE = "1P", "Single phase"
    THREE = "3P", "Three phase"


class SwapSlot(models.TextChoices):
    PANEL = "panel", "Panel"
    INVERTER = "inverter", "Inverter"
    BATTERY = "battery", "Battery"


class StructureType(models.TextChoices):
    """Same vocabulary as ``catalog_structure_spec.structure_type``."""

    FLAT_ROOF = "FLAT_ROOF", "Flat roof"
    ELEVATED = "ELEVATED", "Elevated"
    SHEET_ROOF = "SHEET_ROOF", "Sheet roof"
    GROUND = "GROUND", "Ground mount"


class InstallationType(models.TextChoices):
    """The Flarize installation matrix roof columns (``flat`` / ``sheet`` / ``elevated``)."""

    FLAT = "FLAT", "Flat roof"
    SHEET = "SHEET", "Sheet roof"
    ELEVATED = "ELEVATED", "Elevated"


class StatutoryFeeKind(models.TextChoices):
    KSEB_REGISTRATION = "KSEB_REGISTRATION", "KSEB registration"
    KSEB_METER = "KSEB_METER", "KSEB meter"
    NET_METER_TEST = "NET_METER_TEST", "Net meter test"
    OTHER = "OTHER", "Other"


class OfferType(models.TextChoices):
    FLAT = "FLAT", "Flat amount"
    PERCENT = "PERCENT", "Percentage"


class OfferStatus(models.TextChoices):
    """PLAN's DRAFT/ACTIVE/PAUSED/EXPIRED/ARCHIVED plus Flarize's APPROVED (DV-19)."""

    DRAFT = "DRAFT", "Draft"
    APPROVED = "APPROVED", "Approved"
    ACTIVE = "ACTIVE", "Active"
    PAUSED = "PAUSED", "Paused"
    EXPIRED = "EXPIRED", "Expired"
    ARCHIVED = "ARCHIVED", "Archived"


class OfferSystem(models.TextChoices):
    ALL = "ALL", "All systems"
    ONGRID = "ONGRID", "On-grid"
    HYBRID = "HYBRID", "Hybrid"
    UPGRADE = "UPGRADE", "Upgrade"


class OfferTier(models.TextChoices):
    ALL = "ALL", "All tiers"
    BASE = "BASE", "Base"
    VALUE = "VALUE", "Value"
    PREMIUM = "PREMIUM", "Premium"


class ValidityKind(models.TextChoices):
    DEFAULT = "DEFAULT", "Default (fallback)"
    WINDOW = "WINDOW", "Effective window"


class ValidityWindowStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    INACTIVE = "INACTIVE", "Inactive"


class ReleaseStatus(models.TextChoices):
    PUBLISHED = "PUBLISHED", "Published"
    SUPERSEDED = "SUPERSEDED", "Superseded"


def in_choices(field: str, choices) -> Q:
    return Q(**{f"{field}__in": list(choices.values)})
