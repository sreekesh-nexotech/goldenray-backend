from django.db import models
from django.db.models import Q


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


class ConfigStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    PUBLISHED = "PUBLISHED", "Published"
    SUPERSEDED = "SUPERSEDED", "Superseded"


OPEN_STATUSES = (ConfigStatus.DRAFT, ConfigStatus.SUBMITTED)
CURRENT_STATUSES = (ConfigStatus.APPROVED, ConfigStatus.PUBLISHED)


class LineSource(models.TextChoices):
    SLOT = "SLOT", "Template slot"
    MANUAL = "MANUAL", "Pinned for this pack"
    FIXED = "FIXED", "Fixed item"
    STRUCTURE = "STRUCTURE", "Structure (flat roof)"


class ReleaseStatus(models.TextChoices):
    PUBLISHED = "PUBLISHED", "Published"
    SUPERSEDED = "SUPERSEDED", "Superseded"


def in_choices(field: str, choices) -> Q:
    return Q(**{f"{field}__in": list(choices.values)})
