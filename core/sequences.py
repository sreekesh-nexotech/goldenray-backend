"""Race-free, gap-free document numbers (PLAN §2.1 ``core_sequence_counter``).

``next_number(kind)`` must run inside the caller's transaction: the counter row is locked with
``SELECT … FOR UPDATE`` and advanced in the same transaction as the document insert, so a rolled-back document
also rolls back its number (no gaps) and two concurrent documents never share one (no duplicates). The counter row
is created on first use with ``INSERT … ON CONFLICT DO NOTHING``.

Built-in formats:

========  =====================  =====================================================
kind      period_key             number
========  =====================  =====================================================
QUO       ''                     ``GR-<n>``            (continues the Flarize counter)
AGR       FY, e.g. ``2026-27``   ``AGR-<FY>-<nnnn>``   (Indian FY April–March, D-11)
SV        ``YYYYMMDD``           ``SV-YYYYMMDD-NNNN``  (per-day counter)
LEAD      ''                     ``L-<n>``
PROJ      ''                     ``PROJ-<n>``
========  =====================  =====================================================

Other apps may use their own kinds by passing ``fmt`` (a ``str.format`` template with ``{n}`` and ``{period}``
or a callable ``(n, period) -> str``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from django.db import transaction
from django.utils import timezone

from core.models import SequenceCounter

_KIND_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,15}$")
_PERIOD_RE = re.compile(r"^[A-Za-z0-9-]{0,16}$")

Formatter = Callable[[int, str], str]


def fiscal_year_key(day: date | None = None) -> str:
    """Indian financial year (April–March) label: 2026-09-28 → ``2026-27``; 2027-02-01 → ``2026-27``."""
    day = day or timezone.localdate()
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def day_key(day: date | None = None) -> str:
    return (day or timezone.localdate()).strftime("%Y%m%d")


@dataclass(frozen=True)
class SequenceSpec:
    formatter: Formatter
    period: Callable[[date | None], str] | None = None  # None = one global counter


SPECS: dict[str, SequenceSpec] = {
    "QUO": SequenceSpec(lambda n, period: f"GR-{n}"),
    "AGR": SequenceSpec(lambda n, period: f"AGR-{period}-{n:04d}", fiscal_year_key),
    "SV": SequenceSpec(lambda n, period: f"SV-{period}-{n:04d}", day_key),
    "LEAD": SequenceSpec(lambda n, period: f"L-{n}"),
    "PROJ": SequenceSpec(lambda n, period: f"PROJ-{n}"),
}


def _validate(kind: str, period_key: str) -> None:
    if not _KIND_RE.match(kind or ""):
        raise ValueError(f"Invalid sequence kind {kind!r}: 1-16 upper-case letters, digits or underscores.")
    if not _PERIOD_RE.match(period_key):
        raise ValueError(f"Invalid period_key {period_key!r}: up to 16 letters, digits or dashes.")


def _require_transaction() -> None:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Sequence numbers must be taken inside the caller's transaction (@transaction.atomic).")


def next_value(kind: str, period_key: str = "") -> int:
    """Reserve and return the next integer for ``(kind, period_key)``."""
    _validate(kind, period_key)
    _require_transaction()
    SequenceCounter.objects.bulk_create([SequenceCounter(kind=kind, period_key=period_key, next_value=1)], ignore_conflicts=True)
    counter = SequenceCounter.objects.select_for_update().get(kind=kind, period_key=period_key)
    value = counter.next_value
    SequenceCounter.objects.filter(kind=kind, period_key=period_key).update(next_value=value + 1)
    return value


def resolve_period(kind: str, period_key: str | None = None, *, on: date | None = None) -> str:
    """The period key a number will be taken in: explicit, else derived from the kind's rule, else global ('')."""
    if period_key is not None:
        return period_key
    spec = SPECS.get(kind)
    return spec.period(on) if spec and spec.period else ""


def next_number(kind: str, period_key: str | None = None, fmt: str | Formatter | None = None, *, on: date | None = None) -> str:
    """Reserve the next number of ``kind`` and format it (see the module table)."""
    period = resolve_period(kind, period_key, on=on)
    if fmt is None:
        spec = SPECS.get(kind)
        if spec is None:
            raise ValueError(f"No built-in format for sequence kind {kind!r}; pass fmt=.")
        formatter: Formatter = spec.formatter
    elif isinstance(fmt, str):
        template = fmt
        formatter = lambda n, p: template.format(n=n, period=p)  # noqa: E731
    else:
        formatter = fmt
    return formatter(next_value(kind, period), period)


def ensure_next_value_at_least(kind: str, value: int, period_key: str = "") -> int:
    """Move a counter forward so the next number is at least ``value`` (importers continuing a legacy counter).

    Never moves a counter backwards. Returns the counter's resulting ``next_value``.
    """
    _validate(kind, period_key)
    _require_transaction()
    if value < 1:
        raise ValueError("value must be >= 1")
    SequenceCounter.objects.bulk_create([SequenceCounter(kind=kind, period_key=period_key, next_value=value)], ignore_conflicts=True)
    counter = SequenceCounter.objects.select_for_update().get(kind=kind, period_key=period_key)
    if counter.next_value < value:
        SequenceCounter.objects.filter(kind=kind, period_key=period_key).update(next_value=value)
        return value
    return counter.next_value
