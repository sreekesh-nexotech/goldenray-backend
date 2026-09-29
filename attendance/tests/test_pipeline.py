"""End to end (A8): an agent upload and an ADMS push are stored here, announced on the outbox, and the debounced
recompute — never the terminal's or the agent's request — writes the final days."""

from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.models import AttendanceDay, RawPunch, RecomputeRequest
from attendance.services import recompute
from attendance.tests.conftest import events
from core.services.flags import set_flag
from devices.services.devices import enable_adms
from devices.tests.factories import AgentFactory

pytestmark = pytest.mark.django_db


def test_an_agent_upload_becomes_stored_days(world, drain_outbox):
    agent = AgentFactory(office=world.ho)
    world.d1.agent = agent
    world.d1.save()
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {agent.token}")
    records = [
        {"device_record_uid": 1, "pin": "1", "device_time": "2026-09-14T09:31:00", "status": 15, "punch": 255},
        {"device_record_uid": 2, "pin": "1", "device_time": "2026-09-14T18:35:00", "status": 15, "punch": 255},
        {"device_record_uid": 3, "pin": "1", "device_time": "2026-09-15T09:29:00", "status": 15, "punch": 255},
        {"device_record_uid": 4, "pin": "77", "device_time": "2026-09-14T09:00:00", "status": 15, "punch": 255},
    ]
    response = client.post("/api/agent/v1/sync/attendance/", {"device": str(world.d1.uid), "records": records}, format="json")
    assert response.status_code == 200 and response.json()["new"] == 4
    assert RawPunch.objects.count() == 4 and not AttendanceDay.objects.exists()  # nothing computed in the request
    [event] = events("attendance.punches_ingested")
    assert event["employee_uids"] == [str(world.asha.uid)] and event["unmapped_pins"] == ["77"]
    drain_outbox()
    assert RecomputeRequest.objects.filter(employee=world.asha).exists() and not AttendanceDay.objects.exists()
    recompute.run_due(at=timezone.now() + dt.timedelta(minutes=5))
    days = dict(AttendanceDay.objects.filter(employee=world.asha).values_list("work_date", "status"))
    assert days == {dt.date(2026, 9, 13): "WEEKLY_OFF", dt.date(2026, 9, 14): "PRESENT"}  # today (the 15th) is never stored


def test_an_adms_push_is_stored_once_and_announced(world):
    set_flag("ADMS_RECEIVER", enabled=True, user=None)
    _, token = enable_adms(world.d2, user=None)
    body = "1\t2026-09-14 09:00:00\t255\t15\t0\t0\n"
    client = APIClient()
    for _ in range(2):  # the terminal resends
        response = client.post(f"/iclock/{token}/cdata?SN={world.d2.serial_number}&table=ATTLOG&Stamp=1", data=body, content_type="text/plain")
        assert response.status_code == 200 and response.content.startswith(b"OK")
    row = RawPunch.objects.get()
    assert (row.source, row.pin, row.status_code, row.punch_code) == ("ADMS_PUSH", "1", 15, 255)
    assert row.punch_at == dt.datetime(2026, 9, 14, 5, 0, tzinfo=dt.timezone.utc)  # 09:00 on the Dubai terminal's clock
    assert [event["employee_uids"] for event in events("attendance.punches_ingested")] == [[str(world.chitra.uid)]]
