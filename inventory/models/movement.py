"""``inventory_movement`` *(append-only)* and the ``inventory_balance`` view (PLAN §2.3, flag ``INVENTORY_STOCK``).

A movement is one stock event: ``qty`` (always positive) of a component moving IN to or OUT of a location for a
``reason``, optionally pointing at what caused it (``ref_type`` + ``ref_uid``: ``procurement.batch_line`` for a
received purchase, ``projects.project`` for an issue …), performed ``at`` a time ``by`` somebody.

Append-only is enforced twice, like ``audit_log``:

* in the database — ``UPDATE, DELETE, TRUNCATE`` are revoked from the application role (``DB_APP_ROLE``) by the
  migration and by ``manage.py ensure_inventory_append_only`` (``inventory.services.privileges``);
* in the application — the model refuses to save an existing row, to delete, soft-delete or version-update, and its
  queryset refuses ``update()``/``delete()``. Rows are written only by ``inventory.services.movements``.

A mistake is corrected by a new movement (an ``ADJUST`` with a note), never by editing history. The table still
carries the ``BaseModel`` columns (PLAN notation: it is not marked *no base*); ``deleted_at`` stays NULL and
``version`` 1 for ever.

``inventory_balance`` is a Postgres VIEW (created in ``0001_initial``): the signed sum of the movements per
(component, location). :class:`Balance` maps it read-only (unmanaged; composite key).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import BaseModel
from core.models.base import BaseQuerySet

BATCH_LINE_REF = "procurement.batch_line"
REF_TYPE_PATTERN = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"


class AppendOnlyError(RuntimeError):
    """Raised when code tries to change or remove a stock movement."""


class Direction(models.TextChoices):
    IN = "IN", "In"
    OUT = "OUT", "Out"


class Reason(models.TextChoices):
    PURCHASE = "PURCHASE", "Purchase"
    ISSUE_TO_PROJECT = "ISSUE_TO_PROJECT", "Issue to project"
    RETURN = "RETURN", "Return"
    ADJUST = "ADJUST", "Adjustment"


#: Directions each reason may take (also a database CHECK). RETURN goes both ways: back from a project or site (IN)
#: or back to the supplier (OUT).
REASON_DIRECTIONS: dict[str, tuple[str, ...]] = {
    Reason.PURCHASE: (Direction.IN,),
    Reason.ISSUE_TO_PROJECT: (Direction.OUT,),
    Reason.RETURN: (Direction.IN, Direction.OUT),
    Reason.ADJUST: (Direction.IN, Direction.OUT),
}


class MovementQuerySet(BaseQuerySet):
    def update(self, **kwargs):
        raise AppendOnlyError("inventory_movement is append-only: rows are never updated.")

    def delete(self):
        raise AppendOnlyError("inventory_movement is append-only: rows are never deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise AppendOnlyError("inventory_movement is append-only: rows are never updated.")

    def update_or_create(self, defaults=None, create_defaults=None, **kwargs):
        raise AppendOnlyError("inventory_movement is append-only: rows are never updated.")

    def _raw_delete(self, using):
        raise AppendOnlyError("inventory_movement is append-only: rows are never deleted.")

    _raw_delete.queryset_only = True


class MovementManager(models.Manager.from_queryset(MovementQuerySet)):
    """Every row: movements are never soft-deleted, so ``objects`` and ``all_objects`` are the same set."""


class Movement(BaseModel):
    # PROTECT: stock history keeps its component (components are only soft-deleted in practice).
    # related_name="+": catalog gains no reverse accessor (inventory reads catalog, never the reverse).
    component = models.ForeignKey("catalog.Component", on_delete=models.PROTECT, related_name="+")
    # PROTECT: a location with history cannot disappear (it is soft-deleted, and only with nothing in stock).
    location = models.ForeignKey("inventory.Location", on_delete=models.PROTECT, related_name="movements")
    qty = models.DecimalField(max_digits=12, decimal_places=3, help_text="Always positive; the direction gives the sign.")
    direction = models.CharField(max_length=3, choices=Direction.choices)
    reason = models.CharField(max_length=16, choices=Reason.choices)
    ref_type = models.CharField(max_length=64, blank=True, default="", help_text="What caused it, as <app>.<model> (e.g. procurement.batch_line).")
    ref_uid = models.UUIDField(null=True, blank=True)
    at = models.DateTimeField(default=timezone.now, help_text="When the stock moved (may be earlier than created_at).")
    # SET_NULL: attribution only (who moved the stock; the batch committer for received purchases).
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.TextField(blank=True, default="", help_text="Required for ADJUST (the adjustment's justification).")

    objects = MovementManager()
    all_objects = MovementManager()

    class Meta:
        db_table = "inventory_movement"
        ordering = ["-at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(qty__gt=0), name="inventory_movement_qty_positive"),
            models.CheckConstraint(condition=Q(direction__in=Direction.values), name="inventory_movement_direction_valid"),
            models.CheckConstraint(condition=Q(reason__in=Reason.values), name="inventory_movement_reason_valid"),
            models.CheckConstraint(
                condition=Q(reason=Reason.PURCHASE, direction=Direction.IN) | Q(reason=Reason.ISSUE_TO_PROJECT, direction=Direction.OUT) | Q(reason__in=[Reason.RETURN, Reason.ADJUST]),
                name="inventory_movement_reason_direction",
            ),
            models.CheckConstraint(
                condition=(Q(ref_type="") & Q(ref_uid__isnull=True)) | (Q(ref_type__regex=REF_TYPE_PATTERN) & Q(ref_uid__isnull=False)),
                name="inventory_movement_ref_pair",
            ),
            models.CheckConstraint(condition=~Q(reason=Reason.ADJUST) | ~Q(note=""), name="inventory_movement_adjust_has_note"),
            # One receipt per procurement batch line: the batch_committed handler is at-least-once (standard §7.2).
            models.UniqueConstraint(fields=["ref_type", "ref_uid"], condition=Q(ref_type=BATCH_LINE_REF), name="inventory_movement_batch_line_uniq"),
        ]
        indexes = [
            # Balance of one component at one location (the OUT guard) and the balances view's grouping.
            models.Index(fields=["component", "location"], name="inventory_mov_comp_loc_idx"),
            # Movement list screens: newest first, overall and per location.
            models.Index(fields=["-at", "-id"], name="inventory_mov_at_idx"),
            models.Index(fields=["location", "-at"], name="inventory_mov_location_at_idx"),
            models.Index(fields=["ref_type", "ref_uid"], name="inventory_mov_ref_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.direction} {self.qty} of {self.component_id} @ {self.location_id} ({self.reason})"

    @property
    def signed_qty(self):
        return self.qty if self.direction == Direction.IN else -self.qty

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError("inventory_movement is append-only: rows are never updated.")
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("inventory_movement is append-only: rows are never deleted.")

    def versioned_update(self, user=None, **values) -> None:
        raise AppendOnlyError("inventory_movement is append-only: rows are never updated.")

    def soft_delete(self, user=None) -> None:
        raise AppendOnlyError("inventory_movement is append-only: correct a movement with an ADJUST, never delete it.")

    def restore(self, user=None) -> None:
        raise AppendOnlyError("inventory_movement is append-only: rows are never updated.")


class Balance(models.Model):
    """``inventory_balance`` (Postgres VIEW, read-only): the stock of one component at one location.

    ``qty`` = Σ IN − Σ OUT; ``qty_in`` / ``qty_out`` the two sums; ``movement_count`` and ``last_movement_at`` over
    the movements of the pair. Only pairs with at least one movement have a row.
    """

    pk = models.CompositePrimaryKey("component", "location")
    # DO_NOTHING without a database constraint: a view has no foreign keys; the relations only let the ORM join.
    component = models.ForeignKey("catalog.Component", on_delete=models.DO_NOTHING, db_constraint=False, related_name="+")
    location = models.ForeignKey("inventory.Location", on_delete=models.DO_NOTHING, db_constraint=False, related_name="+")
    qty = models.DecimalField(max_digits=18, decimal_places=3)
    qty_in = models.DecimalField(max_digits=18, decimal_places=3)
    qty_out = models.DecimalField(max_digits=18, decimal_places=3)
    movement_count = models.IntegerField()
    last_movement_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "inventory_balance"
        ordering = ["component_id", "location_id"]
        default_permissions = ()

    def __str__(self) -> str:
        return f"{self.component_id} @ {self.location_id}: {self.qty}"
