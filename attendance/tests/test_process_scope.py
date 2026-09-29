"""B-8 (docs/decisions/business-defaults.md): ``attendance/process/`` and ``…/recalculate/`` apply the caller's
attendance record scope to ``employee_uids`` (fail closed). An employee outside the scope answers 404 ``not_found``
as the scoped timeline does; without ``employee_uids`` only the people in scope are recomputed."""

from __future__ import annotations

import datetime as dt

import pytest

from attendance.models import AttendanceDay
from attendance.tests.conftest import MANAGER_GRANTS, at
from attendance.tests.factories import punch

pytestmark = pytest.mark.django_db

PROCESS = "/api/v1/attendance/process/"
RECALCULATE = "/api/v1/attendance/recalculate/"
WINDOW = {"date_from": "2026-09-14", "date_to": "2026-09-14"}


@pytest.fixture
def office_manager(auth_client, make_user, world):
    """A Branch Office Manager allowed to recompute (``attendance.manage``) within the office scope."""
    from hr.tests.factories import EmployeeFactory

    user = make_user(grants={**MANAGER_GRANTS, "attendance": ["view", "export", "manage"]}, scopes={"employees": "office", "attendance": "office"})
    EmployeeFactory(code="M002", full_name="Meera Manager", office=world.br, user=user, joined_on=dt.date(2026, 1, 1))
    return auth_client(user)


@pytest.fixture
def unlinked_manager(auth_client, make_user, world):
    """Office scope but no employee record of its own: nothing is in scope (fail closed)."""
    return auth_client(make_user(grants={"attendance": ["view", "manage"]}, scopes={"attendance": "office"}))


@pytest.fixture
def punches(world):
    punch(world.d1, "1", at(14, 9, 30))  # Asha, head office
    punch(world.d2, "1", at(14, 9, 30))  # Chitra, branch


@pytest.mark.parametrize("path", [PROCESS, RECALCULATE])
def test_an_employee_outside_the_scope_is_not_found(office_manager, world, punches, path):
    response = office_manager.post(path, {**WINDOW, "employee_uids": [str(world.chitra.uid), str(world.asha.uid)]}, format="json")
    assert response.status_code == 404 and response.json()["code"] == "not_found", response.json()
    assert not AttendanceDay.objects.exists()  # nothing recomputed, not even the in-scope person


@pytest.mark.parametrize("path", [PROCESS, RECALCULATE])
def test_an_employee_in_scope_is_recomputed(office_manager, world, punches, path):
    response = office_manager.post(path, {**WINDOW, "employee_uids": [str(world.chitra.uid)]}, format="json")
    assert response.status_code == 200, response.json()
    assert set(AttendanceDay.objects.values_list("employee__code", flat=True)) == {"E003"}


@pytest.mark.parametrize("path", [PROCESS, RECALCULATE])
def test_without_employee_uids_only_the_people_in_scope_are_recomputed(office_manager, world, punches, path):
    assert office_manager.post(path, WINDOW, format="json").status_code == 200
    codes = set(AttendanceDay.objects.values_list("employee__code", flat=True))
    assert "E003" in codes and not codes & {"E001", "E002"}


@pytest.mark.parametrize("path", [PROCESS, RECALCULATE])
def test_a_caller_without_an_employee_record_recomputes_nobody(unlinked_manager, world, punches, path):
    assert unlinked_manager.post(path, {**WINDOW, "employee_uids": [str(world.asha.uid)]}, format="json").status_code == 404
    assert unlinked_manager.post(path, WINDOW, format="json").status_code == 200
    assert not AttendanceDay.objects.exists()


def test_scope_all_still_recomputes_everyone(hr_client, world, punches):
    assert hr_client.post(PROCESS, WINDOW, format="json").status_code == 200
    assert {"E001", "E003"} <= set(AttendanceDay.objects.values_list("employee__code", flat=True))
