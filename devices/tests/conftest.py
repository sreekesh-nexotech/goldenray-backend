"""devices test fixtures: grants of the seeded roles, an in-memory punch store, agent clients, the ADMS flag."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from core.models import OutboxEvent
from core.services.flags import set_flag
from devices.services import punch_sink
from devices.tests.factories import AgentFactory

ADMIN_GRANTS = {"devices": "*", "employees": "*", "hr_setup": "*"}
HR_GRANTS = {"devices": ["view", "sync"], "employees": "*", "hr_setup": "*", "attendance": "*", "leave": "*"}


class MemorySink(punch_sink.PunchSink):
    """A punch store standing in for the attendance package (dedup by key, per-device queries)."""

    installed = True

    def __init__(self):
        self.punches: dict[str, punch_sink.Punch] = {}

    def store(self, punches):
        result = punch_sink.SinkResult(received=len(punches))
        for punch in punches:
            if punch.dedup_key in self.punches:
                result.duplicate += 1
                continue
            self.punches[punch.dedup_key] = punch
            result.new += 1
            result.new_punches.append(punch)
        return result

    def of(self, device):
        return [punch for punch in self.punches.values() if punch.device_id == device.pk]

    def status(self, device):
        rows = self.of(device)
        uids = [punch.device_record_uid for punch in rows if punch.device_record_uid is not None]
        return {"stored_records": len(rows), "highest_device_record_uid": max(uids) if uids else None, "latest_device_time": max((punch.device_time for punch in rows), default=None)}

    def pin_activity(self, device):
        by_pin: dict[str, list] = {}
        for punch in self.of(device):
            by_pin.setdefault(punch.pin, []).append(punch.device_time)
        return [{"pin": pin, "punch_count": len(times), "first_punch_at": min(times), "last_punch_at": max(times)} for pin, times in by_pin.items()]

    def observed_codes(self):
        counts: dict[tuple, int] = {}
        for punch in self.punches.values():
            for field, value in (("status", punch.status_code), ("punch", punch.punch_code)):
                counts[(field, value)] = counts.get((field, value), 0) + 1
        return [{"field": field, "raw_value": value, "count": count, "device_platform": "", "firmware_version": ""} for (field, value), count in counts.items()]


@pytest.fixture
def sink():
    store = punch_sink.register(MemorySink())
    yield store
    punch_sink.reset()


@pytest.fixture(autouse=True)
def _null_sink():
    punch_sink.reset()
    yield
    punch_sink.reset()


@pytest.fixture
def admin_user(make_user):
    return make_user(grants=ADMIN_GRANTS, scopes={"employees": "all"})


@pytest.fixture
def admin_client(auth_client, admin_user):
    return auth_client(admin_user)


@pytest.fixture
def hr_client(auth_client, make_user):
    """PLAN §3.2 HR: devices view/sync only."""
    return auth_client(make_user(grants=HR_GRANTS, scopes={"employees": "all", "attendance": "all", "leave": "all"}))


@pytest.fixture
def viewer_client(auth_client, make_user):
    return auth_client(make_user(grants={"devices": ["view"]}))


@pytest.fixture
def agent(db):
    return AgentFactory()


def agent_client_for(agent) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {agent.token}")
    return client


@pytest.fixture
def agent_client(agent):
    return agent_client_for(agent)


@pytest.fixture
def adms_on(db):
    set_flag("ADMS_RECEIVER", enabled=True, user=None)


def events(event_type: str) -> list[dict]:
    return [row.payload for row in OutboxEvent.objects.filter(event_type=event_type).order_by("id")]
