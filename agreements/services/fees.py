"""The KSEB registration fee of an agreement, from pricing (Plan 2 §3.1: the page's ``kseb`` list became
``pricing_statutory_fee`` KSEB_REGISTRATION rows, one per capacity band).

The applicable fee is the KSEB_REGISTRATION band with the smallest ``capacity_kw_max`` that still covers the plant
(an open band — no maximum — last), a row for the agreement's phase winning over an any-phase row of the same band.

* from a quotation: the fee pinned in the quotation's PriceRelease (``payload.statutory_fees``), linked to the live
  ``pricing_statutory_fee`` row it came from when that row still exists; a release that pins no applicable fee (e.g.
  PriceRelease #1 from the Flarize data, which has none) falls back to the current rows;
* a blank agreement: the current ``pricing_statutory_fee`` rows (effective today).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone

KIND = "KSEB_REGISTRATION"


@dataclass(frozen=True)
class Fee:
    label: str
    amount: Decimal
    row_id: int | None


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def choose(candidates: list[dict], *, phase: str, capacity_kw: Decimal | None) -> dict | None:
    """The applicable candidate (``{kind, label, phase, capacity_kw_max, amount, …}``) or ``None``."""
    if capacity_kw is None:
        return None
    eligible = []
    for fee in candidates:
        if fee.get("kind") != KIND or fee.get("phase") not in (None, "", phase):
            continue
        maximum = _decimal(fee.get("capacity_kw_max"))
        if maximum is not None and maximum < capacity_kw:
            continue
        open_band = maximum is None
        eligible.append(((open_band, maximum or Decimal(0), 0 if fee.get("phase") == phase else 1), fee))
    if not eligible:
        return None
    return min(eligible, key=lambda item: item[0])[1]


def _row_for(candidate: dict):
    from pricing.models import StatutoryFee

    capacity = _decimal(candidate.get("capacity_kw_max"))
    today = timezone.localdate()
    rows = StatutoryFee.objects.filter(kind=KIND, phase=candidate.get("phase") or None, capacity_kw_max=capacity, effective_from__lte=today)
    return rows.filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today)).order_by("-effective_from", "-id").first()


def from_release(price_release, *, phase: str, capacity_kw) -> Fee | None:
    candidates = list(((price_release.payload or {}).get("statutory_fees") or [])) if price_release is not None else []
    chosen = choose(candidates, phase=phase, capacity_kw=_decimal(capacity_kw))
    if chosen is None:
        return None
    row = _row_for(chosen)
    return Fee(
        label=str(chosen.get("label") or "")[:120], amount=_decimal(chosen.get("amount")) or Decimal(0), row_id=row.pk if row is not None and row.amount == _decimal(chosen.get("amount")) else None
    )


def current(*, phase: str, capacity_kw) -> Fee | None:
    from pricing.models import StatutoryFee

    today = timezone.localdate()
    rows = StatutoryFee.objects.filter(kind=KIND, effective_from__lte=today).filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
    candidates = [{"kind": row.kind, "label": row.label, "phase": row.phase, "capacity_kw_max": row.capacity_kw_max, "amount": row.amount, "row": row} for row in rows]
    chosen = choose(candidates, phase=phase, capacity_kw=_decimal(capacity_kw))
    if chosen is None:
        return None
    return Fee(label=chosen["label"][:120], amount=chosen["amount"], row_id=chosen["row"].pk)


def by_amount(amount: Decimal | None):
    """The current KSEB registration row charging ``amount`` (legacy records store only the amount)."""
    from pricing.models import StatutoryFee

    if amount is None:
        return None
    return StatutoryFee.objects.filter(kind=KIND, amount=amount, effective_to__isnull=True).order_by("capacity_kw_max", "id").first()
