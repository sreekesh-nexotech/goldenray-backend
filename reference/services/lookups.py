"""Reference reads for the website and for other contexts (calculators, leads, installations).

These are the documented reads other packages use instead of querying ``reference_*`` tables themselves:

* :func:`find_pincode` / :func:`district_of` / :func:`pincodes_in_district` — the legacy
  ``Pincode.objects.filter(pincode=…).first().district`` and "every pincode of a district" lookups;
* :func:`current_tariffs` / :func:`slab_for_units` — the KSEB slab schedule in force on a day;
* :func:`device_type_by_name` / :func:`vehicle_by_model` — the advanced calculator's name lookups;
* :func:`active_pincode_codes` / :func:`active_device_types` / :func:`active_vehicles` — whole lists, for the
  calculators' cached snapshot (``calculators.services.data``).
"""

from __future__ import annotations

from datetime import date

from django.db.models import Prefetch
from django.utils import timezone

from reference.models import DeviceType, EvCar, EvScooter, KsebTariff, Pincode, PincodeOffice


def find_pincode(code: str) -> Pincode | None:
    """The live, active pincode with its offices (in legacy order), or ``None``."""
    code = (code or "").strip()
    if len(code) != 6 or not code.isdigit():
        return None
    offices = Prefetch("offices", queryset=PincodeOffice.objects.order_by("sort_order", "id"))
    return Pincode.objects.filter(pincode=code, is_active=True).prefetch_related(offices).first()


def district_of(code: str) -> str | None:
    pincode = find_pincode(code)
    return (pincode.district or None) if pincode else None


def pincodes_in_district(district: str) -> list[str]:
    """Every live pincode with at least one post office in ``district`` (case-sensitive, as stored)."""
    if not district:
        return []
    codes = PincodeOffice.objects.filter(district=district, pincode__deleted_at__isnull=True, pincode__is_active=True).values_list("pincode__pincode", flat=True)
    return sorted(set(codes) | set(Pincode.objects.filter(district=district, is_active=True).values_list("pincode", flat=True)))


def _effective(row: KsebTariff) -> date:
    return row.effective_from or date.min


def current_tariffs(*, on: date | None = None, phase: str | None = None) -> list[KsebTariff]:
    """The slab schedule in force on ``on`` (default: today, IST).

    Rows are grouped by ``phase`` (``None`` = every phase); in each group the schedule with the latest
    ``effective_from`` not after ``on`` wins. With ``phase`` given, that phase's schedule is returned, falling back
    to the every-phase schedule.
    """
    on = on or timezone.localdate()
    rows = [row for row in KsebTariff.objects.filter(is_active=True) if _effective(row) <= on]
    groups: dict[str | None, list[KsebTariff]] = {}
    for row in rows:
        groups.setdefault(row.phase, []).append(row)
    schedule: dict[str | None, list[KsebTariff]] = {}
    for key, members in groups.items():
        latest = max(_effective(row) for row in members)
        schedule[key] = sorted((row for row in members if _effective(row) == latest), key=lambda row: (row.slab_from_units, row.id))
    if phase is not None:
        return schedule.get(phase) or schedule.get(None, [])
    return [row for key in sorted(schedule, key=lambda value: value or "") for row in schedule[key]]


def slab_for_units(units: int, *, on: date | None = None, phase: str | None = None) -> KsebTariff | None:
    """The slab whose range contains ``units`` (the legacy ``min_units__lte=units`` highest match)."""
    candidates = [row for row in current_tariffs(on=on, phase=phase) if row.slab_from_units <= units]
    return max(candidates, key=lambda row: row.slab_from_units) if candidates else None


def active_pincode_codes() -> frozenset[str]:
    """Every live, active pincode (the calculators check a visitor's pincode against it)."""
    return frozenset(Pincode.objects.filter(is_active=True).values_list("pincode", flat=True))


def active_device_types() -> list[DeviceType]:
    """Every live, active device type in legacy order (``sort_order`` is the legacy id)."""
    return list(DeviceType.objects.filter(is_active=True).order_by("sort_order", "id"))


def active_vehicles() -> tuple[list[EvCar], list[EvScooter]]:
    """Every live, active EV car and scooter in legacy order."""
    return list(EvCar.objects.filter(is_active=True).order_by("sort_order", "id")), list(EvScooter.objects.filter(is_active=True).order_by("sort_order", "id"))


def device_type_by_name(name: str) -> DeviceType | None:
    return DeviceType.objects.filter(name__iexact=(name or "").strip(), is_active=True).first()


def vehicle_by_model(model: str) -> EvCar | EvScooter | None:
    """An EV car, else an EV scooter, with this exact model name (the legacy advanced-calculator lookup)."""
    return EvCar.objects.filter(model=model, is_active=True).first() or EvScooter.objects.filter(model=model, is_active=True).first()
