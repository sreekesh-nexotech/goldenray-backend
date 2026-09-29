"""Enumerations of the site-inspection tables (PLAN §2.7, Site Inspection V2 spec §D).

Values are the V2 wizard's stored values (upper-case codes), except where the legacy stored display text (vehicle
type labels) or collapsed two fields into one; the legacy importer maps those (``services.legacy_import``).
"""

from __future__ import annotations

from django.db import models


def values(choices: type[models.TextChoices]) -> list[str]:
    return [str(value) for value in choices.values]


class Origin(models.TextChoices):
    PRE_SALE = "PRE_SALE", "Pre-sale"
    AGREEMENT = "AGREEMENT", "Purchase agreement"


class SystemType(models.TextChoices):
    ON_GRID = "ON_GRID", "On-grid"
    HYBRID = "HYBRID", "Hybrid"
    UNDECIDED = "UNDECIDED", "Undecided"


class Status(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    COMPLETED = "COMPLETED", "Completed"
    CUSTOMER_APPROVAL_PENDING = "CUSTOMER_APPROVAL_PENDING", "Customer approval pending"
    APPROVED = "APPROVED", "Approved"
    INSTALLATION_READY = "INSTALLATION_READY", "Installation ready"
    REJECTED = "REJECTED", "Rejected"
    REVISION_REQUIRED = "REVISION_REQUIRED", "Revision required"
    ON_HOLD = "ON_HOLD", "On hold"


class Complexity(models.TextChoices):
    NOT_ASSESSED = "NOT_ASSESSED", "Not assessed"
    ROUTINE = "ROUTINE", "Routine"
    ENGINEERING_REVIEW_REQUIRED = "ENGINEERING_REVIEW_REQUIRED", "Engineering review required"


class RoadAccess(models.TextChoices):
    GOOD = "GOOD", "Good"
    LIMITED = "LIMITED", "Limited"
    DIFFICULT = "DIFFICULT", "Difficult"
    NO_VEHICLE = "NO_VEHICLE", "No vehicle access"


class VehicleType(models.TextChoices):
    CAR = "CAR", "Car"
    PICKUP_VAN = "PICKUP_VAN", "Pickup / Van"
    TRUCK = "TRUCK", "Truck"
    MANUAL = "MANUAL", "Manual movement only"
    NOT_ASSESSED = "NOT_ASSESSED", "Not assessed"


class RoofType(models.TextChoices):
    RCC_FLAT = "RCC_FLAT", "RCC flat"
    TILE = "TILE", "Tile"
    SHEET_METAL = "SHEET_METAL", "Sheet metal"
    OTHER = "OTHER", "Other"


class RoofStrength(models.TextChoices):
    GOOD = "GOOD", "Good"
    REVIEW = "REVIEW", "Needs review"
    POOR = "POOR", "Poor"


class RoofAccessibility(models.TextChoices):
    EASY = "EASY", "Easy"
    RESTRICTED = "RESTRICTED", "Restricted"
    DIFFICULT = "DIFFICULT", "Difficult"
    UNSAFE = "UNSAFE", "Unsafe"


class RoofCondition(models.TextChoices):
    GOOD = "GOOD", "Good"
    FAIR = "FAIR", "Fair"
    NEEDS_REPAIR = "NEEDS_REPAIR", "Needs repair"
    REVIEW_REQUIRED = "REVIEW_REQUIRED", "Review required"


class Level(models.TextChoices):
    """Installation difficulty."""

    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"


class RoofSlope(models.TextChoices):
    FLAT = "FLAT", "Flat"
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    STEEP = "STEEP", "Steep"


class Direction(models.TextChoices):
    N = "N", "North"
    NE = "NE", "North-east"
    E = "E", "East"
    SE = "SE", "South-east"
    S = "S", "South"
    SW = "SW", "South-west"
    W = "W", "West"
    NW = "NW", "North-west"


class Shading(models.TextChoices):
    NONE = "NONE", "None"
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"


class GenerationImpact(models.TextChoices):
    NONE = "NONE", "None"
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"
    REQUIRES_REVIEW = "REQUIRES_REVIEW", "Requires review"


class Phase(models.TextChoices):
    SINGLE = "1P", "Single phase"
    THREE = "3P", "Three phase"
    NOT_CONFIRMED = "NC", "Not confirmed"


class Tariff(models.TextChoices):
    LT_I = "LT_I", "LT I"
    LT_II = "LT_II", "LT II"
    LT_III = "LT_III", "LT III"
    LT_IV = "LT_IV", "LT IV"
    NOT_CONFIRMED = "NOT_CONFIRMED", "Not confirmed"


class Availability(models.TextChoices):
    """Neutral link, termination point, distribution board, earthing (four separate columns)."""

    AVAILABLE = "AVAILABLE", "Available"
    NOT_AVAILABLE = "NOT_AVAILABLE", "Not available"
    NEEDS_MODIFICATION = "NEEDS_MODIFICATION", "Needs modification"


class Suitability(models.TextChoices):
    SUITABLE = "SUITABLE", "Suitable"
    CONDITIONAL = "CONDITIONAL", "Conditional"
    NOT_SUITABLE = "NOT_SUITABLE", "Not suitable"


class StructureDecision(models.TextChoices):
    SUFFICIENT = "SUFFICIENT", "Sufficient"
    ADDITIONAL_REQUIRED = "ADDITIONAL_REQUIRED", "Additional structure required"
    REQUIRES_ENGINEERING_REVIEW = "REQUIRES_ENGINEERING_REVIEW", "Requires engineering review"


class PhotoType(models.TextChoices):
    PANEL_AREA = "PANEL_AREA", "Panel area"
    EQUIPMENT_AREA = "EQUIPMENT_AREA", "Equipment area"
    ROOF = "ROOF", "Roof"
    ACCESS = "ACCESS", "Site access"
    ELECTRICAL = "ELECTRICAL", "Electrical"
    CABLE_ROUTE = "CABLE_ROUTE", "Cable route"
    SHADING = "SHADING", "Shading"
    ADDITIONAL_WORK = "ADDITIONAL_WORK", "Additional work"
    EVIDENCE = "EVIDENCE", "Evidence"
    OTHER = "OTHER", "Other"


class AnnotationType(models.TextChoices):
    PANEL_AREA = "PANEL_AREA", "Proposed panel area"
    EQUIPMENT_AREA = "EQUIPMENT_AREA", "Proposed equipment area"


class GeometrySpace(models.TextChoices):
    IMAGE = "IMAGE", "Image-normalised"
    LEGACY_CONTAINER = "LEGACY_CONTAINER", "Legacy 4:3 container (re-draw)"


class EquipmentType(models.TextChoices):
    ON_GRID_INVERTER = "ON_GRID_INVERTER", "On-grid inverter"
    HYBRID_INVERTER = "HYBRID_INVERTER", "Hybrid inverter"
    HYBRID_BATTERY = "HYBRID_BATTERY", "Hybrid battery"


class AssessmentStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not started"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    PASS = "PASS", "Pass"
    FAIL = "FAIL", "Fail"
    REQUIRES_REVIEW = "REQUIRES_REVIEW", "Requires review"


class AssessmentReview(models.TextChoices):
    NOT_REQUIRED = "NOT_REQUIRED", "Not required"
    PENDING = "PENDING", "Pending"
    RESOLVED = "RESOLVED", "Resolved"
    WAIVED = "WAIVED", "Waived"


class ApprovalStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    SUPERSEDED = "SUPERSEDED", "Superseded"
    EXPIRED = "EXPIRED", "Expired"


class WorkType(models.TextChoices):
    WALKWAY = "WALKWAY", "Walkway"
    LADDER = "LADDER", "Ladder"
    SLIDING_DOOR = "SLIDING_DOOR", "Sliding door"
    ELEVATED_STRUCTURE = "ELEVATED_STRUCTURE", "Elevated structure"
    UNDERGROUND_CABLING = "UNDERGROUND_CABLING", "Underground cabling"
    EXTRA_AC_CABLE = "EXTRA_AC_CABLE", "Additional AC cable"
    EXTRA_DC_CABLE = "EXTRA_DC_CABLE", "Additional DC cable"
    NEW_NEUTRAL_LINK = "NEW_NEUTRAL_LINK", "New neutral link"
    NEW_TERMINATION = "NEW_TERMINATION", "New termination point"
    ADDITIONAL_EARTHING = "ADDITIONAL_EARTHING", "Additional earthing"
    CIVIL_WORK = "CIVIL_WORK", "Civil / waterproofing"
    STRUCTURE_CHANGE = "STRUCTURE_CHANGE", "Structure / support change"
    OTHER = "OTHER", "Other"


class WorkUnit(models.TextChoices):
    NOS = "NOS", "Nos"
    M = "M", "m"
    M2 = "M2", "m²"
    M3 = "M3", "m³"
    JOB = "JOB", "Job"


class WorkStatus(models.TextChoices):
    NONE = "NONE", "None"
    IDENTIFIED = "IDENTIFIED", "Identified"
    ENGINEERING_REVIEW = "ENGINEERING_REVIEW", "Engineering review"
    COST_CALCULATED = "COST_CALCULATED", "Cost calculated"
    CUSTOMER_QUOTE_SENT = "CUSTOMER_QUOTE_SENT", "Customer quote sent"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class ObservationCategory(models.TextChoices):
    GENERAL = "GENERAL", "General"
    STRUCTURE = "STRUCTURE", "Structure"
    CABLE = "CABLE", "Cable"
    EARTHING = "EARTHING", "Earthing"
    ELECTRICAL = "ELECTRICAL", "Electrical"
    CIVIL = "CIVIL", "Civil"
    ACCESS = "ACCESS", "Access"
    SAFETY = "SAFETY", "Safety"
    OTHER = "OTHER", "Other"


class ReviewTrigger(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    EQUIPMENT = "EQUIPMENT", "Equipment assessment"
    STRUCTURE = "STRUCTURE", "Structure decision"
    ROOF = "ROOF", "Roof condition"
    GENERATION = "GENERATION", "Generation impact"


class ReviewDecision(models.TextChoices):
    PENDING = "PENDING", "Pending"
    ROUTINE = "ROUTINE", "Routine"
    REQUIRES_CHANGES = "REQUIRES_CHANGES", "Requires changes"
    RESOLVED = "RESOLVED", "Resolved"


class SnapshotSource(models.TextChoices):
    PRE_SALE = "PRE_SALE", "Pre-sale"
    AGREEMENT = "AGREEMENT", "Agreement"
