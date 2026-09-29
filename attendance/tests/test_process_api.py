"""``attendance/process/``, ``…/process-all/``, ``…/recalculate/`` (manage) and ``attendance/corrections/`` (A12)."""

from __future__ import annotations

import datetime as dt

import pytest

from attendance.models import AttendanceCorrection, AttendanceDay, RecomputeRequest
from attendance.services import recompute
from attendance.tests.conftest import at
from attendance.tests.factories import AttendanceDayFactory, punch
from audit.models import AuditLog
from devices.tests.factories import AgentFactory, DeviceFactory

pytestmark = pytest.mark.django_db

PROCESS = "/api/v1/attendance/process/"
PROCESS_ALL = "/api/v1/attendance/process-all/"
RECALCULATE = "/api/v1/attendance/recalculate/"
CORRECTIONS = "/api/v1/attendance/corrections/"


class TestProcess:
    def test_permissions(self, api_client, manager_client, world):
        assert api_client.post(PROCESS, {}, format="json").status_code == 401
        for path in (PROCESS, PROCESS_ALL, RECALCULATE):
            assert manager_client.post(path, {"date_from": "2026-09-01", "date_to": "2026-09-02"}, format="json").status_code == 403

    def test_a_window_is_recomputed(self, hr_client, world):
        punch(world.d1, "1", at(14, 9, 30))
        response = hr_client.post(PROCESS, {"date_from": "2026-09-13", "date_to": "2026-09-20", "employee_uids": [str(world.asha.uid)]}, format="json")
        assert response.status_code == 200
        body = response.json()
        assert body["employees"] == 1 and body["created"] == 2 and body["skipped_future"] > 0 and body["reason"] == "process"
        assert set(AttendanceDay.objects.values_list("work_date", flat=True)) == {dt.date(2026, 9, 13), dt.date(2026, 9, 14)}

    def test_validation(self, hr_client, world):
        response = hr_client.post(PROCESS, {"date_from": "2026-09-14", "date_to": "2026-09-01"}, format="json")
        assert response.status_code == 400 and "date_to" in response.json()["errors"]
        response = hr_client.post(PROCESS, {"date_from": "2026-01-01", "date_to": "2026-09-01"}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "range_too_long"
        response = hr_client.post(PROCESS, {"date_from": "2026-09-01", "date_to": "2026-09-02", "employee_uids": ["00000000-0000-0000-0000-000000000000"]}, format="json")
        assert response.status_code == 400 and "employee_uids" in response.json()["errors"]

    def test_process_all_is_queued(self, hr_client, world):
        response = hr_client.post(PROCESS_ALL)
        assert response.status_code == 202 and response.json() == {"queued": False, "date_from": None, "date_to": None, "requests": 0}
        punch(world.d1, "1", at(1, 9, 30))
        punch(world.d1, "1", at(14, 18, 30))
        body = hr_client.post(PROCESS_ALL).json()
        assert body["queued"] is True and (body["date_from"], body["date_to"]) == ("2026-08-31", "2026-09-15")
        row = RecomputeRequest.objects.get()
        assert row.all_employees and row.reason == "process_all"
        recompute.run_due()
        assert AttendanceDay.objects.filter(employee=world.asha, work_date=dt.date(2026, 9, 1)).exists()

    def test_recalculate_reports_how_each_terminal_delivers(self, hr_client, world):
        DeviceFactory(name="PUSHER", office=world.ho, adms_enabled=True, adms_token_hash="b" * 64)
        DeviceFactory(name="AGENTED", office=world.ho, agent=AgentFactory(office=world.ho))
        response = hr_client.post(RECALCULATE, {}, format="json")
        assert response.status_code == 200
        body = response.json()
        assert (body["result"]["computed_from"], body["result"]["computed_to"]) == ("2026-09-14", "2026-09-14")
        transports = {item["device"]["name"]: item["transport"] for item in body["devices"]}
        assert transports == {"AGENTED": "AGENT", "BR-01": "NONE", "HO-01": "NONE", "PUSHER": "ADMS"}
        assert body["summary"] == {"devices": 4, "without_transport": 2}
        assert hr_client.post(RECALCULATE, {"date_from": "2026-09-10"}, format="json").json()["result"]["computed_to"] == "2026-09-14"


@pytest.fixture
def day(world):
    return AttendanceDayFactory(employee=world.binu, work_date=dt.date(2026, 9, 14), status="ABSENT", first_in=None, last_out=None, punch_count=0, working_minutes=0, break_minutes=0)


class TestCorrections:
    def test_permissions(self, api_client, staff_client, world, day):
        assert api_client.get(CORRECTIONS).status_code == 401
        response = staff_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "status", "new": "PRESENT", "reason": "x"}, format="json")
        assert response.status_code == 403

    def test_a_correction_pins_the_day_until_revoked(self, hr_client, world, day):
        response = hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "status", "new": "PRESENT", "reason": "Site visit all day", "expected_version": day.version}, format="json")
        assert response.status_code == 201
        body = response.json()
        assert (body["field"], body["old"], body["new"], body["is_active"], body["employee"]["code"], body["work_date"]) == ("status", "ABSENT", "PRESENT", True, "E002", "2026-09-14")
        corrected = AttendanceDay.objects.get(pk=day.pk)
        assert corrected.status == "PRESENT" and corrected.is_corrected and corrected.version == day.version + 1
        assert AuditLog.objects.filter(action="attendance.day_corrected").count() == 1
        # the recompute leaves it alone (A12)
        assert recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), employee_ids=[world.binu.pk], reason="test")["skipped_corrected"] == 1
        assert AttendanceDay.objects.get(pk=day.pk).status == "PRESENT"
        # a second correction of the same field is refused
        again = hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "status", "new": "LATE", "reason": "again"}, format="json")
        assert again.status_code == 409 and again.json()["code"] == "correction_exists"
        # revoke: the value comes back and the day is handed to the recompute
        correction = AttendanceCorrection.objects.get()
        revoked = hr_client.post(f"{CORRECTIONS}{correction.uid}/revoke/", {"reason": "Wrong person", "expected_version": correction.version}, format="json")
        assert revoked.status_code == 200 and revoked.json()["is_active"] is False and revoked.json()["revoke_reason"] == "Wrong person"
        restored = AttendanceDay.objects.get(pk=day.pk)
        assert restored.status == "ABSENT" and not restored.is_corrected
        assert RecomputeRequest.objects.filter(employee=world.binu, reason="correction_revoked").exists()
        twice = hr_client.post(f"{CORRECTIONS}{correction.uid}/revoke/", {"reason": "again"}, format="json")
        assert twice.status_code == 409 and twice.json()["code"] == "correction_revoked"

    def test_clock_corrections_and_value_validation(self, hr_client, world, day):
        ok = hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "first_in", "new": "2026-09-14T09:40:00", "reason": "Punch missed"}, format="json")
        assert ok.status_code == 201
        stored = AttendanceDay.objects.get(pk=day.pk)
        assert stored.first_in == dt.datetime(2026, 9, 14, 9, 40) and stored.missing_out is True
        cases = [
            ("last_out", "2026-09-14T08:00:00"),  # before IN
            ("last_out", "2026-09-14T18:00:00+05:30"),  # a zone is not a wall clock
            ("status", "SICK"),
            ("working_minutes", -5),
            ("is_late", "yes"),
        ]
        for field, value in cases:
            response = hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": field, "new": value, "reason": "x"}, format="json")
            assert response.status_code == 400 and response.json()["code"] == "validation_error", (field, value)
        response = hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "punch_count", "new": 3, "reason": "x"}, format="json")
        assert response.status_code == 400 and "field" in response.json()["errors"]
        response = hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "status", "new": "PRESENT", "reason": "  "}, format="json")
        assert response.status_code == 400

    def test_revoking_one_of_two_keeps_the_day_pinned_and_refuses_contradictions(self, hr_client, world, day):
        hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "first_in", "new": "2026-09-14T09:40:00", "reason": "a"}, format="json")
        hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "last_out", "new": "2026-09-14T18:40:00", "reason": "b"}, format="json")
        first_in = AttendanceCorrection.objects.get(field="first_in")
        conflict = hr_client.post(f"{CORRECTIONS}{first_in.uid}/revoke/", {"reason": "undo"}, format="json")
        assert conflict.status_code == 409 and conflict.json()["code"] == "correction_revoke_conflict"  # an OUT without an IN
        last_out = AttendanceCorrection.objects.get(field="last_out")
        assert hr_client.post(f"{CORRECTIONS}{last_out.uid}/revoke/", {"reason": "undo"}, format="json").status_code == 200
        day.refresh_from_db()
        assert day.last_out is None and day.is_corrected and not RecomputeRequest.objects.exists()

    def test_nobody_corrects_their_own_day(self, auth_client, make_user, world):
        editor = make_user(grants={"attendance": "*"}, scopes={"attendance": "all"})
        own_employee = world.binu
        own_employee.user = editor
        own_employee.save()
        day = AttendanceDayFactory(employee=own_employee, work_date=dt.date(2026, 9, 14), status="ABSENT", first_in=None, last_out=None)
        response = auth_client(editor).post(CORRECTIONS, {"day_uid": str(day.uid), "field": "status", "new": "PRESENT", "reason": "me"}, format="json")
        assert response.status_code == 403 and response.json()["code"] == "self_action_denied"

    def test_stale_version_and_scope(self, hr_client, auth_client, make_user, world, day):
        response = hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "status", "new": "PRESENT", "reason": "x", "expected_version": day.version + 5}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        office_editor = make_user(grants={"attendance": ["view", "edit"]}, scopes={"attendance": "office"})
        world.chitra.user = office_editor
        world.chitra.save()  # an editor in the branch
        response = auth_client(office_editor).post(CORRECTIONS, {"day_uid": str(day.uid), "field": "status", "new": "PRESENT", "reason": "x"}, format="json")
        assert response.status_code == 404
        hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "status", "new": "PRESENT", "reason": "x"}, format="json")
        correction = AttendanceCorrection.objects.get()
        stale = hr_client.post(f"{CORRECTIONS}{correction.uid}/revoke/", {"reason": "x", "expected_version": 99}, format="json")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_version"

    def test_list_filters_and_scope(self, hr_client, staff_client, world, day, django_assert_max_num_queries):
        hr_client.post(CORRECTIONS, {"day_uid": str(day.uid), "field": "status", "new": "PRESENT", "reason": "x"}, format="json")
        own = AttendanceDayFactory(employee=world.asha, work_date=dt.date(2026, 9, 14))
        hr_client.post(CORRECTIONS, {"day_uid": str(own.uid), "field": "overtime_minutes", "new": 30, "reason": "y"}, format="json")
        with django_assert_max_num_queries(10):
            body = hr_client.get(CORRECTIONS).json()
        assert body["count"] == 2
        assert hr_client.get(CORRECTIONS, {"employee": str(world.binu.uid)}).json()["count"] == 1
        assert hr_client.get(CORRECTIONS, {"active": "false"}).json()["count"] == 0
        assert [row["employee"]["code"] for row in staff_client.get(CORRECTIONS).json()["results"]] == ["E001"]
        detail = hr_client.get(f"{CORRECTIONS}{AttendanceCorrection.objects.get(field='overtime_minutes').uid}/").json()
        assert detail["new"] == 30 and detail["created_by"]["uid"] == str(world.hr.uid)
        assert hr_client.post(CORRECTIONS, {"day_uid": "00000000-0000-0000-0000-000000000000", "field": "status", "new": "PRESENT", "reason": "x"}, format="json").status_code == 404
