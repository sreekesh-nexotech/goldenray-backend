"""``pricing/installation-matrix/`` and ``pricing/statutory-fees/`` — small commercial masters (PLAN §2.3, §3.4).

Installation matrix: one exact install cost per (size, phase, installation type) — the cost engine never uses a
nearest size (Flarize C75). Statutory fees: KSEB registration/meter/net-meter fees; one current row per
(kind, phase, capacity band) — ``effective_to`` closes a fee, a new row replaces it.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from pricing.models import InstallationMatrix, StatutoryFee
from pricing.services.common import AUTHORING_NAMESPACE, today

MATRIX_FIELDS = ("size_kw", "phase", "installation_type", "install_cost", "labour_days")
FEE_FIELDS = ("kind", "label", "phase", "capacity_kw_max", "amount", "effective_from", "effective_to")


def matrix_queryset():
    return InstallationMatrix.objects.order_by("size_kw", "phase", "installation_type", "id")


def fees_queryset():
    return StatutoryFee.objects.order_by("kind", "phase", "capacity_kw_max", "-effective_from", "id")


def _save_new(instance, *, user, conflict: Conflict):
    stamp_create(instance, user)
    try:
        with transaction.atomic():
            instance.save()
    except IntegrityError:
        raise conflict from None
    return instance


def _update(instance, values: dict, *, user, conflict: Conflict, fields, action: str):
    values = {name: value for name, value in values.items() if getattr(instance, name) != value}
    if not values:
        return instance
    before = snapshot(instance, fields)
    try:
        with transaction.atomic():
            instance.versioned_update(user, **values)
    except IntegrityError:
        raise conflict from None
    changed_before, changed_after = changes(before, snapshot(instance, fields))
    record(action, obj=instance, actor=user, before=changed_before, after=changed_after)
    bump(AUTHORING_NAMESPACE)
    return instance


# ── installation matrix ────────────────────────────────────────────────────────────────────────────────────────────


def _matrix_conflict() -> Conflict:
    return Conflict("installation_cell_exists", "This size, phase and installation type already has a cost.", errors={"size_kw": ["Already configured."]})


@transaction.atomic
def create_matrix_row(*, user, data: dict) -> InstallationMatrix:
    row = _save_new(InstallationMatrix(**{name: data[name] for name in MATRIX_FIELDS if name in data}), user=user, conflict=_matrix_conflict())
    record("pricing.installation_cost_created", obj=row, actor=user, after=snapshot(row, MATRIX_FIELDS))
    bump(AUTHORING_NAMESPACE)
    return row


@transaction.atomic
def update_matrix_row(instance: InstallationMatrix, *, user, data: dict, expected_version=None) -> InstallationMatrix:
    row = InstallationMatrix.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    return _update(row, {name: data[name] for name in MATRIX_FIELDS if name in data}, user=user, conflict=_matrix_conflict(), fields=MATRIX_FIELDS, action="pricing.installation_cost_updated")


@transaction.atomic
def delete_matrix_row(instance: InstallationMatrix, *, user, expected_version=None) -> None:
    row = InstallationMatrix.objects.select_for_update().get(pk=instance.pk)
    check_version(row, expected_version)
    row.soft_delete(user)
    record("pricing.installation_cost_deleted", obj=row, actor=user, before=snapshot(row, MATRIX_FIELDS))
    bump(AUTHORING_NAMESPACE)


def matrix_cost(size_kw, installation_type: str, phase: str = "") -> InstallationMatrix | None:
    """The exact cell (phase-specific first, then the any-phase row) — never a nearest size."""
    rows = {row.phase: row for row in InstallationMatrix.objects.filter(size_kw=size_kw, installation_type=installation_type, phase__in=[phase, ""])}
    return rows.get(phase) or rows.get("")


# ── statutory fees ─────────────────────────────────────────────────────────────────────────────────────────────────


def _fee_conflict() -> Conflict:
    return Conflict("statutory_fee_exists", "A current fee of this kind already covers this phase and capacity; close it first.", errors={"kind": ["Already configured."]})


def _check_fee_window(values: dict, instance: StatutoryFee | None = None) -> None:
    start = values.get("effective_from", instance.effective_from if instance else None)
    end = values.get("effective_to", instance.effective_to if instance else None)
    if start and end and end < start:
        raise DomainError("validation_error", "effective_to must be on or after effective_from.", errors={"effective_to": ["Before effective_from."]})


@transaction.atomic
def create_fee(*, user, data: dict) -> StatutoryFee:
    values = {name: data[name] for name in FEE_FIELDS if name in data}
    values.setdefault("effective_from", today())
    _check_fee_window(values)
    fee = _save_new(StatutoryFee(**values), user=user, conflict=_fee_conflict())
    record("pricing.statutory_fee_created", obj=fee, actor=user, after=snapshot(fee, FEE_FIELDS))
    bump(AUTHORING_NAMESPACE)
    return fee


@transaction.atomic
def update_fee(instance: StatutoryFee, *, user, data: dict, expected_version=None) -> StatutoryFee:
    fee = StatutoryFee.objects.select_for_update().get(pk=instance.pk)
    check_version(fee, expected_version)
    values = {name: data[name] for name in FEE_FIELDS if name in data}
    _check_fee_window(values, fee)
    return _update(fee, values, user=user, conflict=_fee_conflict(), fields=FEE_FIELDS, action="pricing.statutory_fee_updated")


@transaction.atomic
def delete_fee(instance: StatutoryFee, *, user, expected_version=None) -> None:
    fee = StatutoryFee.objects.select_for_update().get(pk=instance.pk)
    check_version(fee, expected_version)
    fee.soft_delete(user)
    record("pricing.statutory_fee_deleted", obj=fee, actor=user, before=snapshot(fee, FEE_FIELDS))
    bump(AUTHORING_NAMESPACE)


def current_fees():
    return fees_queryset().filter(effective_to__isnull=True)
