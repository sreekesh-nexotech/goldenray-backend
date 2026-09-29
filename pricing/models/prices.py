"""``pricing_price`` — append-only price rows — and the ``pricing_current_price`` view (PLAN §2.3).

A row is never edited: ``effective_to`` is the only column that changes, once, when a newer row of the same component
and kind closes it (in the same transaction as the insert). The partial unique index allows one open row per
(component, kind), so "the current price" is well defined; the database enforces the append-only rule too
(``pricing/migrations/0002_current_price_view_and_append_only.py``: a trigger refuses any other UPDATE and every
DELETE). ``LIST`` is never derived by markup in code (``source`` MARKUP is refused for LIST rows).

Columns beyond the PLAN list (DV, docs/decisions/pricing-procurement.md): ``per_watt`` — the per-watt reference the BOM
and Flarize catalogs carry beside a panel's LIST price.
"""

from django.db import models
from django.db.models import F, Q

from core.models import BaseModel
from pricing.models.choices import PriceKind, PriceSource, in_choices

OPEN = Q(effective_to__isnull=True) & Q(deleted_at__isnull=True)


class PriceQuerySet(models.QuerySet):
    def current(self):
        return self.filter(effective_to__isnull=True, deleted_at__isnull=True)


class PriceManager(models.Manager.from_queryset(PriceQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class Price(BaseModel):
    # PROTECT: a component with price history can never disappear.
    component = models.ForeignKey("catalog.Component", on_delete=models.PROTECT, related_name="prices")
    kind = models.CharField(max_length=12, choices=PriceKind.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="INR")
    gst_inclusive = models.BooleanField(default=False)
    per_watt = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True, help_text="Per-watt reference of a panel LIST price (BOM/Flarize).")
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=12, choices=PriceSource.choices)
    source_ref = models.CharField(max_length=64, blank=True, default="", help_text="Batch line uid, import source row, …")
    # SET_NULL: the supplier is attribution; suppliers are only soft-deleted in practice.
    supplier = models.ForeignKey("procurement.Supplier", null=True, blank=True, on_delete=models.SET_NULL, related_name="prices")
    note = models.TextField(blank=True, default="")
    version_key = models.CharField(max_length=64, blank=True, default="", help_text="e.g. BATCH-2026-004::a1 (Flarize versionId).")

    objects = PriceManager()

    class Meta:
        db_table = "pricing_price"
        ordering = ["component_id", "kind", "-effective_from", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["component", "kind"], condition=OPEN, name="pricing_price_one_current_per_kind"),
            models.UniqueConstraint(fields=["kind", "version_key"], condition=~Q(version_key=""), name="pricing_price_version_key_uniq"),
            models.CheckConstraint(condition=in_choices("kind", PriceKind), name="pricing_price_kind_valid"),
            models.CheckConstraint(condition=in_choices("source", PriceSource), name="pricing_price_source_valid"),
            models.CheckConstraint(condition=~(Q(kind=PriceKind.LIST) & Q(source=PriceSource.MARKUP)), name="pricing_price_list_never_markup"),
            models.CheckConstraint(condition=Q(amount__gte=0), name="pricing_price_amount_not_negative"),
            models.CheckConstraint(condition=Q(per_watt__isnull=True) | Q(per_watt__gte=0), name="pricing_price_per_watt_not_negative"),
            models.CheckConstraint(condition=Q(currency__regex=r"^[A-Z]{3}$"), name="pricing_price_currency_iso"),
            models.CheckConstraint(condition=Q(effective_to__isnull=True) | Q(effective_to__gte=F("effective_from")), name="pricing_price_effective_window"),
        ]
        indexes = [models.Index(F("component"), F("kind"), F("effective_from").desc(), name="pricing_price_comp_kind_from")]

    def __str__(self) -> str:
        return f"{self.component_id}/{self.kind} {self.amount} from {self.effective_from}"

    @property
    def is_current(self) -> bool:
        return self.effective_to is None and self.deleted_at is None


class CurrentPrice(models.Model):
    """``pricing_current_price`` — a Postgres VIEW (unmanaged): the latest open row per (component, kind).

    ``id`` is the underlying ``pricing_price.id``. Authoring screens read it; releases copy from it.
    """

    id = models.BigIntegerField(primary_key=True)
    uid = models.UUIDField()
    # DO_NOTHING / no constraint: a view has no foreign keys; the price row's own FK protects the component.
    component = models.ForeignKey("catalog.Component", on_delete=models.DO_NOTHING, db_constraint=False, related_name="+")
    kind = models.CharField(max_length=12, choices=PriceKind.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3)
    gst_inclusive = models.BooleanField()
    per_watt = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    effective_from = models.DateField()
    source = models.CharField(max_length=12, choices=PriceSource.choices)
    source_ref = models.CharField(max_length=64)
    supplier = models.ForeignKey("procurement.Supplier", null=True, on_delete=models.DO_NOTHING, db_constraint=False, related_name="+")
    note = models.TextField()
    version_key = models.CharField(max_length=64)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "pricing_current_price"
        ordering = ["component_id", "kind"]
        default_permissions = ()

    def __str__(self) -> str:
        return f"{self.component_id}/{self.kind} = {self.amount}"
