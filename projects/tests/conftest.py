"""Fixtures for the projects tests: a checker context over a hand-written Flarize catalog (four components)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from catalog.tests.factories import ComponentFactory


@dataclass
class FakeRelease:
    number: int
    payload: dict


@dataclass
class FakeContext:
    catalog: dict
    price_release: object
    battery_master: dict


CATALOG = {
    "categories": {
        "panel": {"items": [{"id": "p1", "name": "Panel", "status": "ACTIVE", "watt": 540}]},
        "inverter": {"items": [{"id": "i1", "name": "Inverter", "status": "ACTIVE", "kw": 3, "phase": "1P"}]},
        "isolator": {"items": [{"id": "is1", "name": "Isolator", "status": "ACTIVE"}]},
        "structure": {"items": [{"id": "s1", "name": "Structure", "status": "ACTIVE"}]},
    }
}
RELEASE = FakeRelease(7, {"components": {"p1": {"list_price": "15000.00", "landed_cost": "11000.00"}, "i1": {"list_price": "42000.00", "landed_cost": None}}})


@pytest.fixture
def components(db):
    return {sku: ComponentFactory(sku=sku, name=name) for sku, name in (("p1", "Panel"), ("i1", "Inverter"), ("is1", "Isolator"), ("s1", "Structure"))}


@pytest.fixture
def checker(monkeypatch):
    from projects.services import bom_lock

    monkeypatch.setattr(bom_lock, "checker_context", lambda: FakeContext(CATALOG, RELEASE, {}))


@pytest.fixture
def full_lines(components):
    """Only WARN findings (three need an acknowledgement: PBC-D-001, PBC-D-003, PBC-N-001)."""
    roles = {"p1": ("PANEL", 6), "i1": ("INVERTER", 1), "is1": ("AC_ISOLATOR", 1), "s1": ("STRUCTURE", 1)}
    return [{"component_uid": str(components[sku].uid), "role": role, "quantity": qty} for sku, (role, qty) in roles.items()]


@pytest.fixture
def short_lines(full_lines):
    """Panel + inverter only: PBC-K-001 ×2 and PBC-Q-001 BLOCK besides the warnings."""
    return full_lines[:2]


ACKS = [{"rule_code": code, "reason": "String layout confirmed on site"} for code in ("PBC-D-001", "PBC-D-003", "PBC-N-001")]
