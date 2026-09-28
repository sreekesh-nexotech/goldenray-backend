"""Pincode → district lookups behind ``installations/stats`` (the legacy ``installation-stats``).

The pincode list is reference data owned by the ``reference`` app (``reference_pincode``: ``pincode``, ``district`` …,
PLAN §2.8) — a website-content app that sales may read but that must never import sales. So this module reads it
through the app registry (no import), and the directory is replaceable: :func:`register` installs another one (the
parity tests install the legacy ``pincodes`` table; an office-level directory can be installed once reference keeps
per-post-office rows).

Semantics kept from the legacy view (``customer_installation_views.InstallationStatsByPincodeAPIView``):

* the district of a pincode is the district of its first (lowest id) row; a pincode that is not in the list falls
  back to a fixed prefix map (``682`` → ``Ernakulam`` …) and then to ``Kerala``;
* a district's installations are those whose pincode is listed under that district (district names compare exactly,
  so a fallback name such as ``Ernakulam`` matches no listed pincode — as before).
"""

from __future__ import annotations

import logging
from typing import Protocol

from django.apps import apps

logger = logging.getLogger("flarize.leads.pincodes")

LEGACY_PREFIX_DISTRICTS = {
    "688": "Alappuzha",
    "689": "Alappuzha",
    "690": "Kollam",
    "691": "Kollam",
    "695": "Thiruvananthapuram",
    "682": "Ernakulam",
    "683": "Ernakulam",
    "684": "Ernakulam",
    "680": "Thrissur",
    "681": "Thrissur",
    "673": "Kozhikode",
    "674": "Kozhikode",
    "670": "Kannur",
    "671": "Kannur",
    "676": "Malappuram",
    "677": "Malappuram",
    "678": "Palakkad",
    "679": "Palakkad",
    "685": "Idukki",
    "686": "Kottayam",
    "687": "Kottayam",
}
DEFAULT_REGION = "Kerala"


class PincodeDirectory(Protocol):
    def district_for(self, pincode: str) -> str | None:
        """The district of ``pincode``, or ``None`` when the pincode is not listed."""

    def pincodes_in(self, district: str) -> set[str] | None:
        """Every pincode listed under ``district``; ``None`` when no pincode list is available at all."""


class ReferenceDirectory:
    """``reference.Pincode`` (PLAN §2.8) when that model is installed; otherwise no data."""

    def _model(self):
        try:
            model = apps.get_model("reference", "Pincode")
        except LookupError:
            return None
        names = {field.name for field in model._meta.get_fields()}
        return model if {"pincode", "district"} <= names else None

    def district_for(self, pincode: str) -> str | None:
        model = self._model()
        if model is None:
            return None
        return model._default_manager.filter(pincode=pincode).order_by("pk").values_list("district", flat=True).first() or None

    def pincodes_in(self, district: str) -> set[str] | None:
        model = self._model()
        if model is None:
            return None
        return set(model._default_manager.filter(district=district).values_list("pincode", flat=True))


class TableDirectory:
    """A directory over ``(pincode, district, first_id)`` rows — the legacy ``pincodes`` table exported for parity."""

    def __init__(self, rows):
        self._district: dict[str, tuple[int, str]] = {}
        self._pincodes: dict[str, set[str]] = {}
        for pincode, district, first_id in rows:
            current = self._district.get(pincode)
            if current is None or first_id < current[0]:
                self._district[pincode] = (first_id, district)
            self._pincodes.setdefault(district, set()).add(pincode)

    def district_for(self, pincode: str) -> str | None:
        found = self._district.get(pincode)
        return found[1] if found else None

    def pincodes_in(self, district: str) -> set[str] | None:
        return set(self._pincodes.get(district, ()))


_directory: PincodeDirectory = ReferenceDirectory()


def register(directory: PincodeDirectory) -> PincodeDirectory:
    """Install ``directory``; returns the previous one (so tests can restore it)."""
    global _directory
    previous, _directory = _directory, directory
    return previous


def current() -> PincodeDirectory:
    return _directory


def legacy_fallback(pincode: str) -> str:
    prefix = pincode[:3] if len(pincode) >= 3 else ""
    return LEGACY_PREFIX_DISTRICTS.get(prefix, DEFAULT_REGION)


def district_of(pincode: str) -> str:
    """The district the stats report for ``pincode`` (listed district, else the legacy fallback)."""
    return current().district_for(pincode) or legacy_fallback(pincode)
