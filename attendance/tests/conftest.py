"""attendance fixtures: the real punch store, a frozen clock, two offices with terminals and linked PINs, the seeded
HR / Office Manager / Staff grants (PLAN §3.2).

The clock is 2026-09-15 12:00 in Kolkata (Tuesday): 2026-09-14 (Monday) is the last final day there; 2026-09-13 is a
Sunday (weekly off under the default shift).
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from attendance.services import sink
from core.models import OutboxEvent
from devices.services import punch_sink
from devices.tests.factories import DeviceFactory, DeviceUserFactory
from hr.tests.factories import EmployeeFactory, OfficeFactory, ShiftFactory

NOW = "2026-09-15T06:30:00+00:00"  # 12:00 in Asia/Kolkata
TODAY = dt.date(2026, 9, 15)
HR_GRANTS = {"dashboard": ["view"], "employees": "*", "hr_setup": "*", "attendance": "*", "leave": "*"}
MANAGER_GRANTS = {"employees": ["view"], "attendance": ["view", "export"], "leave": ["view", "approve"]}
STAFF_GRANTS = {"dashboard": ["view"], "attendance": ["view"], "leave": ["view", "create"]}


@pytest.fixture(autouse=True)
def _punch_store():
    """The attendance store behind devices' ingestion (devices' own tests reset the global sink to the null one)."""
    previous = punch_sink.get()
    sink.install()
    yield
    punch_sink.register(previous)


@pytest.fixture(autouse=True)
def media_roots(settings, tmp_path):
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.USE_X_ACCEL = False
    return tmp_path


@pytest.fixture
def frozen():
    with freeze_time(NOW) as clock:
        yield clock


@pytest.fixture
def world(db, make_user, frozen):
    """Head office (Kolkata) and a branch (Dubai), one terminal each; PIN 1 is Asha on HO-01 and Chitra on BR-01 (A1)."""
    shift = ShiftFactory(code="GEN", name="General", start_time=dt.time(9, 30), end_time=dt.time(18, 30))
    ho = OfficeFactory(code="HO", name="Head Office", timezone="Asia/Kolkata", default_shift=shift)
    br = OfficeFactory(code="BR", name="Branch", timezone="Asia/Dubai", default_shift=shift)
    d1 = DeviceFactory(name="HO-01", serial_number="NCD0000000001", office=ho)
    d2 = DeviceFactory(name="BR-01", serial_number="NCD0000000002", office=br)
    asha = EmployeeFactory(code="E001", full_name="Asha Menon", office=ho, joined_on=dt.date(2026, 1, 1))
    binu = EmployeeFactory(code="E002", full_name="Binu Joseph", office=ho, joined_on=dt.date(2026, 1, 1))
    chitra = EmployeeFactory(code="E003", full_name="Chitra Nair", office=br, joined_on=dt.date(2026, 1, 1))
    DeviceUserFactory(device=d1, pin="1", employee=asha)
    DeviceUserFactory(device=d1, pin="2", employee=binu)
    DeviceUserFactory(device=d2, pin="1", employee=chitra)
    hr_user = make_user(grants=HR_GRANTS, scopes={"employees": "all", "attendance": "all", "leave": "all"})
    manager = make_user(grants=MANAGER_GRANTS, scopes={"employees": "office", "attendance": "office", "leave": "office"})
    EmployeeFactory(code="M001", full_name="Mona Manager", office=br, user=manager, joined_on=dt.date(2026, 1, 1))
    staff = make_user(grants=STAFF_GRANTS, scopes={"attendance": "self", "leave": "self"})
    asha.user = staff
    asha.save()
    return SimpleNamespace(shift=shift, ho=ho, br=br, d1=d1, d2=d2, asha=asha, binu=binu, chitra=chitra, hr=hr_user, manager=manager, staff=staff)


@pytest.fixture
def hr_client(auth_client, world):
    return auth_client(world.hr)


@pytest.fixture
def manager_client(auth_client, world):
    return auth_client(world.manager)


@pytest.fixture
def staff_client(auth_client, world):
    return auth_client(world.staff)


def events(event_type: str) -> list[dict]:
    return [row.payload for row in OutboxEvent.objects.filter(event_type=event_type).order_by("id")]


def at(day: int, hour: int, minute: int = 0, second: int = 0, month: int = 9) -> dt.datetime:
    """A terminal wall-clock time in September 2026."""
    return dt.datetime(2026, month, day, hour, minute, second)
