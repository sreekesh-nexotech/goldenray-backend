"""``procurement_batch`` (+ ``_line``, ``_charge``) — purchase batches (PLAN §2.4).

A batch is edited only while DRAFT. Commit is one transaction: the charges are allocated by purchase-value proportion
(``engines.cost.allocate_landed``), two ``pricing_price`` rows (PURCHASE + LANDED, ``version_key``
``<batch number>::<sku>``) are written per line and the previous open rows closed. A COMMITTED batch is immutable; a
correction is a **reversing batch** (``reverses`` → the original; lines mirror the original with negative quantities
and the prices the original set are replaced by the ones it had superseded).

Columns beyond the PLAN list (DV, docs/decisions/pricing-procurement.md): ``effective_from`` and ``commit_reason`` (the
price versions' effective date and the recorded "why" — Flarize ``effectiveFrom`` / ``changeReason``), ``reverses``,
``is_seed`` (Flarize seed/test batches), ``other_charges_declared`` (Flarize control total); on lines
``allocated_charges`` and ``allocation_pct`` (the allocation result kept beside the landed cost).
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)
ALLOCATION_METHOD = "PURCHASE_VALUE_PROPORTION"


class BatchStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    COMMITTED = "COMMITTED", "Committed"
    CANCELLED = "CANCELLED", "Cancelled"


class ChargeKind(models.TextChoices):
    FREIGHT = "FREIGHT", "Freight"
    INSURANCE = "INSURANCE", "Insurance"
    HANDLING = "HANDLING", "Handling"
    DUTY = "DUTY", "Duty"
    OTHER = "OTHER", "Other"


class Batch(BaseModel):
    number = models.CharField(max_length=32)
    # PROTECT: a supplier with batches cannot disappear.
    supplier = models.ForeignKey("procurement.Supplier", on_delete=models.PROTECT, related_name="batches")
    invoice_no = models.CharField(max_length=64, blank=True, default="")
    invoice_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=BatchStatus.choices, default=BatchStatus.DRAFT)
    committed_at = models.DateTimeField(null=True, blank=True)
    # SET_NULL: attribution only.
    committed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    charges_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    allocation_method = models.CharField(max_length=32, default=ALLOCATION_METHOD)
    note = models.TextField(blank=True, default="")
    effective_from = models.DateField(null=True, blank=True)
    commit_reason = models.TextField(blank=True, default="")
    # PROTECT: a reversed batch stays forever (it is referenced by its reversal).
    reverses = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversals")
    is_seed = models.BooleanField(default=False)
    other_charges_declared = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "procurement_batch"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["number"], condition=LIVE, name="procurement_batch_number_live_uniq"),
            models.UniqueConstraint(fields=["reverses"], condition=LIVE & Q(reverses__isnull=False), name="procurement_batch_one_reversal"),
            models.CheckConstraint(condition=Q(status__in=BatchStatus.values), name="procurement_batch_status_valid"),
            models.CheckConstraint(condition=Q(allocation_method=ALLOCATION_METHOD), name="procurement_batch_allocation_method_valid"),
            models.CheckConstraint(condition=~Q(status=BatchStatus.COMMITTED) | (Q(committed_at__isnull=False) & Q(effective_from__isnull=False)), name="procurement_batch_committed_stamped"),
            models.CheckConstraint(condition=Q(reverses__isnull=True) | ~Q(reverses=F("id")), name="procurement_batch_not_own_reversal"),
            models.CheckConstraint(condition=Q(number__regex=r"^[A-Z0-9][A-Z0-9_.-]{0,31}$"), name="procurement_batch_number_format"),
        ]
        indexes = [models.Index(fields=["status", "-created_at"], name="procurement_batch_status_created")]

    def __str__(self) -> str:
        return f"{self.number} ({self.status})"

    @property
    def is_reversal(self) -> bool:
        return self.reverses_id is not None


class BatchLine(BaseModel):
    # CASCADE: a line is a true child of its batch.
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="lines")
    # PROTECT: a purchased component can never disappear.
    component = models.ForeignKey("catalog.Component", on_delete=models.PROTECT, related_name="batch_lines")
    qty = models.DecimalField(max_digits=12, decimal_places=3)
    unit_purchase_price = models.DecimalField(max_digits=14, decimal_places=2)
    line_value = models.GeneratedField(expression=F("qty") * F("unit_purchase_price"), output_field=models.DecimalField(max_digits=27, decimal_places=5), db_persist=True)
    landed_unit_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    allocated_charges = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    allocation_pct = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)
    # SET_NULL: the price rows written at commit (pricing_price is append-only, so the link never dangles in practice).
    price_row = models.ForeignKey("pricing.Price", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    landed_row = models.ForeignKey("pricing.Price", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "procurement_batch_line"
        ordering = ["batch_id", "id"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "component"], condition=LIVE, name="procurement_batch_line_component_uniq"),
            models.CheckConstraint(condition=~Q(qty=0), name="procurement_batch_line_qty_not_zero"),
            models.CheckConstraint(condition=Q(unit_purchase_price__gte=0), name="procurement_batch_line_price_not_negative"),
            models.CheckConstraint(condition=Q(landed_unit_cost__isnull=True) | Q(landed_unit_cost__gte=0), name="procurement_batch_line_landed_not_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.batch_id}/{self.component_id} × {self.qty}"


class BatchCharge(BaseModel):
    # CASCADE: a charge is a true child of its batch.
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="charges")
    kind = models.CharField(max_length=16, choices=ChargeKind.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    note = models.TextField(blank=True, default="")

    class Meta:
        db_table = "procurement_batch_charge"
        ordering = ["batch_id", "id"]
        constraints = [models.CheckConstraint(condition=Q(kind__in=ChargeKind.values), name="procurement_batch_charge_kind_valid")]

    def __str__(self) -> str:
        return f"{self.batch_id}/{self.kind} {self.amount}"
