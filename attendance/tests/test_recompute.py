"""The recompute (engine v4 over the stored inputs), its debounce queue (A8) and finalise_day (A7)."""

from __future__ import annotations

import datetime as dt

import pytest
from django.core.cache import cache
from django.utils import timezone

from attendance import events as attendance_events
from attendance.models import AttendanceDay, RecomputeRequest
from attendance.services import recompute
from attendance.tests.conftest import TODAY, at
from attendance.tests.factories import AttendanceDayFactory, punch
from audit.models import AuditLog
from core.outbox import Event
from hr.tests.factories import EmployeeFactory, HolidayFactory, LeaveRecordFactory

pytestmark = pytest.mark.django_db


def day_of(employee, day: dt.date) -> AttendanceDay:
    return AttendanceDay.objects.get(employee=employee, work_date=day)


class TestRecompute:
    def test_days_are_computed_from_linked_punches(self, world):
        punch(world.d1, "1", at(14, 9, 35))
        punch(world.d1, "1", at(14, 9, 36))  # a double scan (A3)
        punch(world.d1, "1", at(14, 18, 30))
        punch(world.d2, "1", at(14, 9, 0))  # PIN 1 on another terminal is another person (A1)
        result = recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="test")
        asha = day_of(world.asha, dt.date(2026, 9, 14))
        assert (asha.status, asha.first_in, asha.last_out, asha.punch_count, asha.working_minutes, asha.break_minutes) == ("PRESENT", at(14, 9, 35), at(14, 18, 30), 2, 475, 60)
        assert len(asha.source_raw_ids) == 2 and len(asha.ignored_raw_ids) == 1 and asha.processing_version == "v4"
        assert asha.office_id == world.ho.pk and asha.shift_id == world.shift.pk and asha.first_device_id == world.d1.pk and asha.missing_out is False
        assert day_of(world.binu, dt.date(2026, 9, 14)).status == "ABSENT"
        chitra = day_of(world.chitra, dt.date(2026, 9, 14))
        assert (chitra.status, chitra.first_in, chitra.missing_out) == ("PRESENT", at(14, 9, 0), True)
        assert result["created"] == 4 and result["employees"] == 4 and result["raw_punches_considered"] == 4
        assert AuditLog.objects.filter(action="attendance.recomputed").count() == 1

    def test_today_and_later_are_never_stored(self, world):
        punch(world.d1, "1", at(15, 9, 30))
        result = recompute.recompute(date_from=dt.date(2026, 9, 13), date_to=dt.date(2026, 9, 30), reason="test")
        assert not AttendanceDay.objects.filter(work_date__gte=TODAY).exists()
        assert result["computed_to"] <= "2026-09-16" and result["skipped_future"] > 0
        # the weekly off (Sunday) and the final Monday are stored for everyone
        assert set(AttendanceDay.objects.filter(employee=world.binu).values_list("work_date", "status")) == {(dt.date(2026, 9, 13), "WEEKLY_OFF"), (dt.date(2026, 9, 14), "ABSENT")}

    def test_each_office_has_its_own_today(self, world, frozen):
        frozen.move_to("2026-09-14T20:30:00+00:00")  # 02:00 on the 15th in Kolkata, 00:30 on the 15th in Dubai
        recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="test")
        assert AttendanceDay.objects.filter(employee=world.asha, work_date=dt.date(2026, 9, 14)).exists()
        assert AttendanceDay.objects.filter(employee=world.chitra, work_date=dt.date(2026, 9, 14)).exists()
        frozen.move_to("2026-09-14T19:00:00+00:00")  # 00:30 on the 15th in Kolkata, 23:00 on the 14th in Dubai
        AttendanceDay.all_objects.all().delete()
        recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="test")
        assert AttendanceDay.objects.filter(employee=world.asha, work_date=dt.date(2026, 9, 14)).exists()
        assert not AttendanceDay.objects.filter(employee=world.chitra, work_date=dt.date(2026, 9, 14)).exists()

    def test_holidays_and_leave_come_from_hr(self, world):
        HolidayFactory(date=dt.date(2026, 9, 10), name="Onam", office=world.ho)
        LeaveRecordFactory(employee=world.binu, date_from=dt.date(2026, 9, 11), date_to=dt.date(2026, 9, 11), status="APPROVED")
        LeaveRecordFactory(employee=world.asha, date_from=dt.date(2026, 9, 11), date_to=dt.date(2026, 9, 11), status="PENDING")
        recompute.recompute(date_from=dt.date(2026, 9, 10), date_to=dt.date(2026, 9, 11), reason="test")
        assert day_of(world.asha, dt.date(2026, 9, 10)).status == "HOLIDAY"
        assert day_of(world.chitra, dt.date(2026, 9, 10)).status == "ABSENT"  # another office's holiday
        assert day_of(world.binu, dt.date(2026, 9, 11)).status == "ON_LEAVE"
        assert day_of(world.asha, dt.date(2026, 9, 11)).status == "ABSENT"  # pending leave does not count

    def test_only_changes_are_written_and_versions_move(self, world):
        recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="test")
        first = day_of(world.asha, dt.date(2026, 9, 14))
        again = recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="test")
        assert again["created"] == again["updated"] == 0 and again["unchanged"] == 4
        punch(world.d1, "1", at(14, 9, 30))
        third = recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="test")
        assert third["updated"] == 1
        updated = day_of(world.asha, dt.date(2026, 9, 14))
        assert updated.status == "PRESENT" and updated.version == first.version + 1 and updated.uid == first.uid

    def test_corrected_days_are_left_alone(self, world):
        corrected = AttendanceDayFactory(employee=world.asha, work_date=dt.date(2026, 9, 14), status="PRESENT", is_corrected=True)
        result = recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="test")
        assert result["skipped_corrected"] == 1
        assert day_of(world.asha, dt.date(2026, 9, 14)).version == corrected.version

    def test_days_outside_the_employment_are_removed_unless_corrected(self, world):
        stale = AttendanceDayFactory(employee=world.binu, work_date=dt.date(2026, 9, 7), status="ABSENT")
        kept = AttendanceDayFactory(employee=world.binu, work_date=dt.date(2026, 9, 8), status="PRESENT", is_corrected=True)
        world.binu.joined_on = dt.date(2026, 9, 10)
        world.binu.save()
        result = recompute.recompute(date_from=dt.date(2026, 9, 7), date_to=dt.date(2026, 9, 8), reason="test")
        assert result["removed"] == 1
        assert not AttendanceDay.objects.filter(pk=stale.pk).exists() and AttendanceDay.all_objects.get(pk=stale.pk).deleted_at is not None
        assert AttendanceDay.objects.filter(pk=kept.pk).exists()

    def test_people_who_left_get_their_last_days_and_nothing_after(self, world):
        world.binu.is_active = False
        world.binu.left_on = dt.date(2026, 9, 11)
        world.binu.save()
        gone = EmployeeFactory(office=world.ho, is_active=False)
        recompute.recompute(date_from=dt.date(2026, 9, 10), date_to=dt.date(2026, 9, 14), reason="test")
        assert set(AttendanceDay.objects.filter(employee=world.binu).values_list("work_date", flat=True)) == {dt.date(2026, 9, 10), dt.date(2026, 9, 11)}
        assert not AttendanceDay.objects.filter(employee=gone).exists()

    def test_scoping_the_recompute_and_empty_ranges(self, world):
        result = recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), employee_ids=[world.asha.pk], reason="test")
        assert result["employees"] == 1 and AttendanceDay.objects.count() == 1
        result = recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), office_ids=[world.br.pk], reason="test")
        assert result["employees"] == 2
        assert recompute.recompute(date_from=dt.date(2026, 9, 20), date_to=dt.date(2026, 9, 25), reason="test")["employees"] == 0
        assert recompute.recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), employee_ids=[], reason="test")["employees"] == 0

    def test_a_long_range_keeps_the_latest_days(self, world, settings):
        settings.ATTENDANCE_MAX_RECOMPUTE_DAYS = 5
        result = recompute.recompute(date_from=dt.date(2026, 1, 1), date_to=dt.date(2026, 9, 14), reason="test")
        assert result["not_recomputed_before"] == "2026-01-01" and result["computed_from"] > "2026-09-08"


class TestDebounceQueue:
    def test_events_queue_requests_and_one_run_does_the_work(self, world, django_capture_on_commit_callbacks):
        punch(world.d1, "1", at(14, 9, 30))
        payload = {"device_uid": str(world.d1.uid), "employee_uids": [str(world.asha.uid)], "date_from": "2026-09-14", "date_to": "2026-09-14", "source": "AGENT_PUSH", "new": 1}
        with django_capture_on_commit_callbacks() as callbacks:
            attendance_events.punches_ingested(Event(1, "attendance.punches_ingested", "", None, payload, timezone.now(), 0))
            attendance_events.punches_ingested(Event(2, "attendance.punches_ingested", "", None, payload, timezone.now(), 0))
        assert len(callbacks) == 1  # one task per debounce window
        rows = list(RecomputeRequest.objects.order_by("id"))
        assert len(rows) == 2 and rows[0].date_from == dt.date(2026, 9, 13) and rows[0].date_to == dt.date(2026, 9, 15) and rows[0].reason == "punches_agent_push"
        assert recompute.run_due()["requests"] == 0  # not due yet (debounce)
        result = recompute.run_due(at=timezone.now() + dt.timedelta(minutes=2))
        assert result == {"requests": 2, "calls": 1, "failed": 0}
        assert not RecomputeRequest.objects.exists()
        assert day_of(world.asha, dt.date(2026, 9, 14)).status == "PRESENT"
        assert not AttendanceDay.objects.filter(employee=world.binu).exists()  # only the people the punches belong to

    def test_unmapped_punches_and_malformed_events_queue_nothing(self, world):
        attendance_events.punches_ingested(Event(1, "attendance.punches_ingested", "", None, {"employee_uids": [], "date_from": "2026-09-14", "date_to": "2026-09-14"}, timezone.now(), 0))
        attendance_events.punches_ingested(Event(1, "attendance.punches_ingested", "", None, {"employee_uids": [str(world.asha.uid)], "date_from": "x"}, timezone.now(), 0))
        attendance_events.attendance_inputs_changed(Event(1, "hr.attendance_inputs_changed", "", None, {"office_uid": None, "date_from": None}, timezone.now(), 0))
        assert not RecomputeRequest.objects.exists()

    @pytest.mark.parametrize("scope", ["employees", "office", "everyone", "unknown_office"])
    def test_hr_input_changes(self, world, scope):
        base = {"date_from": "2026-09-10", "date_to": "2026-09-14", "reason": "holiday_changed"}
        payload = {
            "employees": {**base, "employee_uids": [str(world.binu.uid)]},
            "office": {**base, "office_uid": str(world.br.uid)},
            "everyone": {**base, "office_uid": None},
            "unknown_office": {**base, "office_uid": "00000000-0000-0000-0000-000000000000"},
        }[scope]
        attendance_events.attendance_inputs_changed(Event(1, "hr.attendance_inputs_changed", "", None, payload, timezone.now(), 0))
        if scope == "unknown_office":
            assert not RecomputeRequest.objects.exists()
            return
        row = RecomputeRequest.objects.get()
        assert (row.employee_id, row.office_id, row.all_employees, row.reason) == {
            "employees": (world.binu.pk, None, False, "holiday_changed"),
            "office": (None, world.br.pk, False, "holiday_changed"),
            "everyone": (None, None, True, "holiday_changed"),
        }[scope]
        recompute.run_due(at=timezone.now() + dt.timedelta(minutes=2))
        people = {"employees": {world.binu.pk}, "office": {world.chitra.pk, world.manager.employee.pk}, "everyone": None}[scope]
        stored = set(AttendanceDay.objects.values_list("employee_id", flat=True))
        assert stored == people if people is not None else len(stored) == 4

    def test_merging_requests(self, world):
        recompute.request_recompute(employee_ids=[world.asha.pk], date_from=dt.date(2026, 9, 10), date_to=dt.date(2026, 9, 11), reason="a", delay_seconds=0)
        recompute.request_recompute(employee_ids=[world.asha.pk, world.binu.pk], date_from=dt.date(2026, 9, 12), date_to=dt.date(2026, 9, 12), reason="b", delay_seconds=0)
        recompute.request_recompute(office_id=world.br.pk, date_from=dt.date(2026, 9, 12), date_to=dt.date(2026, 9, 12), reason="c", delay_seconds=0)
        recompute.request_recompute(date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 12), reason="d", all_employees=True, delay_seconds=0)
        assert recompute.request_recompute(employee_ids=[], date_from=dt.date(2026, 9, 1), date_to=dt.date(2026, 9, 1), reason="none") == 0
        calls = recompute._merge(list(RecomputeRequest.objects.all()))
        assert calls[0] == {"date_from": dt.date(2026, 9, 12), "date_to": dt.date(2026, 9, 14)}
        assert {"date_from": dt.date(2026, 9, 12), "date_to": dt.date(2026, 9, 12), "office_ids": [world.br.pk]} in calls
        assert {"date_from": dt.date(2026, 9, 10), "date_to": dt.date(2026, 9, 12), "employee_ids": [world.asha.pk]} in calls
        assert {"date_from": dt.date(2026, 9, 12), "date_to": dt.date(2026, 9, 12), "employee_ids": [world.binu.pk]} in calls
        assert recompute.pending()["pending"] == 5

    def test_a_failing_run_keeps_the_work_with_its_error(self, world, monkeypatch):
        recompute.request_recompute(employee_ids=[world.asha.pk], date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="a", delay_seconds=0)

        def broken(**kwargs):
            raise RuntimeError("engine down")

        monkeypatch.setattr(recompute, "recompute", broken)
        assert recompute.run_due()["failed"] == 1
        row = RecomputeRequest.objects.get()
        assert row.attempts == 1 and "engine down" in row.last_error and row.due_at > timezone.now()
        assert recompute.pending()["failing"] == 1

    def test_the_task_runs_what_is_due(self, world):
        from attendance.tasks import run_due_recomputes

        recompute.request_recompute(employee_ids=[world.asha.pk], date_from=dt.date(2026, 9, 14), date_to=dt.date(2026, 9, 14), reason="a", delay_seconds=0)
        assert run_due_recomputes.run()["requests"] == 1


class TestFinaliseDay:
    def test_each_office_is_finalised_once_after_half_past_midnight_on_its_clock(self, world, frozen):
        frozen.move_to("2026-09-14T18:45:00+00:00")  # 00:15 on the 15th in Kolkata; 22:45 on the 14th in Dubai
        results = recompute.finalise_due()
        # Kolkata before 00:30: the day before yesterday; Dubai (late on the 14th): its yesterday, the 13th
        assert {(item["office_id"], item["work_date"]) for item in results} == {(world.ho.pk, "2026-09-13"), (world.br.pk, "2026-09-13")}
        frozen.move_to("2026-09-14T19:05:00+00:00")  # 00:35 in Kolkata
        results = recompute.finalise_due()
        assert [(item["office_id"], item["work_date"]) for item in results] == [(world.ho.pk, "2026-09-14")]
        assert recompute.finalise_due() == []  # once per office and date
        assert day_of(world.binu, dt.date(2026, 9, 14)).status == "ABSENT"
        assert not AttendanceDay.objects.filter(work_date__gte=dt.date(2026, 9, 15)).exists()
        assert not AttendanceDay.objects.filter(employee=world.chitra, work_date=dt.date(2026, 9, 14)).exists()

    def test_people_without_an_office_use_the_platform_zone(self, world):
        loner = EmployeeFactory(office=None, joined_on=dt.date(2026, 1, 1))
        results = recompute.finalise_due()
        assert any(item["office_id"] is None for item in results)
        assert AttendanceDay.objects.filter(employee=loner, work_date=dt.date(2026, 9, 14)).exists()

    def test_a_failing_office_is_retried_on_the_next_tick(self, world, monkeypatch):
        calls = []

        def flaky(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("boom")

        monkeypatch.setattr(recompute, "recompute", flaky)
        assert recompute.finalise_due() == []
        assert not cache.get(f"attendance:finalised:{world.ho.pk}:2026-09-14")

    def test_the_beat_task(self, world):
        from attendance.tasks import finalise_days

        assert {item["work_date"] for item in finalise_days.run()} == {"2026-09-14"}
