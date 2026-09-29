"""HR test fixtures: the seeded HR / Office Manager / Staff grants, media roots in a per-test directory, outbox events."""

import pytest

from core.models import OutboxEvent
from hr.tests.factories import EmployeeFactory, OfficeFactory

HR_GRANTS = {"dashboard": ["view"], "employees": "*", "hr_setup": "*", "attendance": "*", "leave": "*", "media": ["view", "create"]}


@pytest.fixture(autouse=True)
def media_roots(settings, tmp_path):
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.PUBLIC_MEDIA_URL = "/media/public/"
    settings.USE_X_ACCEL = False
    return tmp_path


@pytest.fixture
def hr_user(make_user):
    """PLAN §3.2 HR: employees, hr_setup, attendance, leave in full; scope all."""
    return make_user(grants=HR_GRANTS, scopes={"employees": "all", "attendance": "all", "leave": "all"})


@pytest.fixture
def hr_client(auth_client, hr_user):
    return auth_client(hr_user)


@pytest.fixture
def office():
    return OfficeFactory(code="HO", name="Head Office")


@pytest.fixture
def manager(make_user, office):
    """PLAN §3.2 Office Manager: employees.view, leave view/approve — office scope — linked to an employee in ``office``."""
    user = make_user(grants={"employees": ["view"], "attendance": ["view", "export"], "leave": ["view", "approve"]}, scopes={"employees": "office", "attendance": "office", "leave": "office"})
    EmployeeFactory(office=office, user=user, full_name="Mona Manager")
    return user


@pytest.fixture
def staff(make_user, office):
    """PLAN §3.2 Staff: dashboard, attendance.view, leave view/create — self scope — linked to an employee."""
    user = make_user(grants={"dashboard": ["view"], "attendance": ["view"], "leave": ["view", "create"]}, scopes={"attendance": "self", "leave": "self"})
    EmployeeFactory(office=office, user=user, full_name="Sam Staff")
    return user


def events(event_type: str) -> list[dict]:
    return [row.payload for row in OutboxEvent.objects.filter(event_type=event_type).order_by("id")]
