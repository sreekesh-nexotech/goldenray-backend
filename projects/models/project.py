"""``projects_project`` (PLAN §2.6, kept minimal by D-5).

Lifecycle (POST sub-resources only)::

    PLANNED ──lock-bom/──▶ IN_PROGRESS ──commission/──▶ COMMISSIONED ──close/──▶ CLOSED
       └──────────── cancel/ (from PLANNED or IN_PROGRESS) ──────────▶ CANCELLED

The quotation version, agreement, site inspection and lead are referenced by ``uid`` without a foreign key (DV-128):
those contexts are built in parallel and projects learns about them from outbox events only.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)


class ProjectStatus(models.TextChoices):
    PLANNED = "PLANNED", "Planned"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    COMMISSIONED = "COMMISSIONED", "Commissioned"
    CLOSED = "CLOSED", "Closed"
    CANCELLED = "CANCELLED", "Cancelled"


class SystemType(models.TextChoices):
    ON_GRID = "ON_GRID", "On-grid"
    HYBRID = "HYBRID", "Hybrid"
    OFF_GRID = "OFF_GRID", "Off-grid"
    UNDECIDED = "UNDECIDED", "Undecided"


class KsebStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not started"
    APPLIED = "APPLIED", "Applied"
    FEASIBILITY_OK = "FEASIBILITY_OK", "Feasibility approved"
    METER_INSTALLED = "METER_INSTALLED", "Net meter installed"
    SYNCHRONISED = "SYNCHRONISED", "Synchronised"
    REJECTED = "REJECTED", "Rejected"


class Phase(models.TextChoices):
    ONE = "1P", "Single phase"
    THREE = "3P", "Three phase"


def _in(field: str, choices, *, blank: bool = False) -> Q:
    values = list(choices.values) + ([""] if blank else [])
    return Q(**{f"{field}__in": values})


class Project(BaseModel):
    number = models.CharField(max_length=24, help_text="PROJ-<n> (core.sequences PROJ).")
    # PROTECT: a customer with projects is merged, never deleted (registered with customers.services.merge).
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="+")
    quotation_version_uid = models.UUIDField(null=True, blank=True, help_text="quotations_version.uid (no FK, DV-128).")
    agreement_uid = models.UUIDField(null=True, blank=True, help_text="agreements_agreement.uid (no FK, DV-128).")
    site_inspection_uid = models.UUIDField(null=True, blank=True, help_text="site_inspections_inspection.uid (no FK, DV-128).")
    lead_uid = models.UUIDField(null=True, blank=True, help_text="leads_lead.uid carried by the released inspection (no FK, DV-128).")
    title = models.CharField(max_length=160, blank=True, default="")
    status = models.CharField(max_length=14, choices=ProjectStatus.choices, default=ProjectStatus.PLANNED)
    # SET_NULL: attribution only (the Project Head responsible).
    head = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    system_type = models.CharField(max_length=10, choices=SystemType.choices, blank=True, default="")
    tier = models.CharField(max_length=10, blank=True, default="")
    size_kw = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    phase = models.CharField(max_length=4, choices=Phase.choices, blank=True, default="")
    bom_lock = models.JSONField(null=True, blank=True, help_text="The locked BOM snapshot (lines, frozen prices, engineering verdict, acknowledgements).")
    bom_locked_at = models.DateTimeField(null=True, blank=True)
    # SET_NULL: attribution only.
    bom_locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # SET_NULL: the checker run the lock was judged by (engineering keeps its runs).
    engineering_run = models.ForeignKey("engineering.Run", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cost_inputs = models.JSONField(default=dict, blank=True, help_text="Project cost inputs (pricing_internal).")
    scheduled_on = models.DateField(null=True, blank=True)
    commissioned_on = models.DateField(null=True, blank=True)
    commissioned_at = models.DateTimeField(null=True, blank=True)
    kseb_status = models.CharField(max_length=16, choices=KsebStatus.choices, default=KsebStatus.NOT_STARTED)
    closed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True, default="")
    note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "projects_project"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["number"], condition=LIVE, name="projects_project_number_uniq"),
            models.UniqueConstraint(
                fields=["site_inspection_uid"],
                condition=LIVE & Q(site_inspection_uid__isnull=False) & ~Q(status="CANCELLED"),
                name="projects_project_one_per_inspection",
            ),
            models.CheckConstraint(condition=_in("status", ProjectStatus), name="projects_project_status_valid"),
            models.CheckConstraint(condition=_in("system_type", SystemType, blank=True), name="projects_project_system_type_valid"),
            models.CheckConstraint(condition=_in("phase", Phase, blank=True), name="projects_project_phase_valid"),
            models.CheckConstraint(condition=_in("kseb_status", KsebStatus), name="projects_project_kseb_status_valid"),
            models.CheckConstraint(condition=Q(size_kw__isnull=True) | Q(size_kw__gt=0), name="projects_project_size_positive"),
            models.CheckConstraint(
                condition=Q(status__in=["PLANNED", "CANCELLED"]) | (Q(bom_lock__isnull=False) & Q(bom_locked_at__isnull=False)),
                name="projects_project_started_has_bom_lock",
            ),
            models.CheckConstraint(condition=~Q(status="PLANNED") | Q(bom_lock__isnull=True), name="projects_project_planned_unlocked"),
            models.CheckConstraint(condition=~Q(status__in=["COMMISSIONED", "CLOSED"]) | Q(commissioned_on__isnull=False), name="projects_project_commissioned_on"),
            models.CheckConstraint(condition=~Q(status="CLOSED") | Q(closed_at__isnull=False), name="projects_project_closed_at"),
            models.CheckConstraint(condition=~Q(status="CANCELLED") | (Q(cancelled_at__isnull=False) & ~Q(cancel_reason="")), name="projects_project_cancel_reason"),
        ]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="projects_project_status_idx"),
            models.Index(fields=["customer"], name="projects_project_customer_idx"),
            models.Index(fields=["head"], name="projects_project_head_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.number} ({self.status})"
