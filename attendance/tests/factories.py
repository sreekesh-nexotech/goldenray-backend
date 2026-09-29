"""attendance factories: raw punches go through the real store (``attendance.services.sink.insert``)."""

from __future__ import annotations

import datetime as dt

import factory
from django.utils import timezone

from attendance.models import AttendanceCorrection, AttendanceDay, RawPunch
from attendance.services import sink
from devices.services import punch_sink
from devices.tests.factories import DeviceFactory, DeviceUserFactory  # noqa: F401 - re-exported
from hr.tests.factories import EmployeeFactory


def punch(device, pin, when: dt.datetime, *, status=None, punch_code=None, source="AGENT_PUSH", record_uid=None, raw_payload=None) -> RawPunch:
    """Store one punch at the terminal wall-clock time ``when`` (naive) and return the row."""
    serial = device.serial_number or device.expected_serial or ""
    key = punch_sink.dedup_key(serial, str(pin), when, status, punch_code)
    item = punch_sink.Punch(
        device_id=device.pk,
        device_uid=str(device.uid),
        device_serial=serial,
        office_timezone=device.office.timezone if device.office_id else "Asia/Kolkata",
        pin=str(pin),
        device_time=when,
        status_code=status,
        punch_code=punch_code,
        source=source,
        dedup_key=key,
        device_record_uid=record_uid,
        raw_payload=raw_payload or {},
        received_at=timezone.now(),
    )
    sink.insert([item])
    return RawPunch.objects.get(dedup_key=key, device_time=when)


class AttendanceDayFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AttendanceDay

    employee = factory.SubFactory(EmployeeFactory)
    work_date = dt.date(2026, 9, 1)
    status = AttendanceDay.Status.PRESENT
    first_in = dt.datetime(2026, 9, 1, 9, 30)
    last_out = dt.datetime(2026, 9, 1, 18, 30)
    punch_count = 2
    working_minutes = 480
    break_minutes = 60
    processing_version = "v4"
    computed_at = factory.LazyFunction(timezone.now)
    office = factory.LazyAttribute(lambda day: day.employee.office)


class AttendanceCorrectionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AttendanceCorrection

    day = factory.SubFactory(AttendanceDayFactory)
    field = "status"
    old = "ABSENT"
    new = "PRESENT"
    reason = "Forgot to punch"
