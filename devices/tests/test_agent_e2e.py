"""The office agent (essl-agent/, the real package) against this server over HTTP, with a simulated terminal.

Proves the two halves speak the same protocol: the agent adopts the label-registered device from /config/, reads the
terminal (pyzk replaced by an in-memory double), announces it (binding + verification), uploads the user table and
the punches, and later reports an impostor at the same address — every step through /api/agent/v1/.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from devices.models import Device, DeviceUser, SyncLog
from devices.tests.factories import AgentFactory, DeviceFactory

pytestmark = pytest.mark.django_db(transaction=True)
AGENT_PACKAGE = Path(__file__).resolve().parents[2] / "essl-agent"
SERIAL = "NCD8253601138"


class FakeTerminal:
    def __init__(self, serial):
        self.serial = serial
        self.users = [types.SimpleNamespace(uid=1, user_id="1", name="Asha", privilege=14, password="", group_id="1", card=0)]
        self.logs = [types.SimpleNamespace(uid=uid, user_id="1", timestamp=datetime(2026, 9, 22, 9, uid), status=15, punch=0) for uid in (1, 2, 3)]

    def connect(self):
        terminal = self

        class Connection:
            def get_serialnumber(self):
                return terminal.serial

            def get_mac(self):
                return "00:17:61:12:9c:49"

            def get_time(self):
                return datetime(2026, 9, 22, 9, 30)

            def get_users(self):
                return terminal.users

            def get_attendance(self):
                return terminal.logs

            def disconnect(self):
                pass

        return Connection()


@pytest.fixture
def essl_agent(monkeypatch):
    monkeypatch.syspath_prepend(str(AGENT_PACKAGE))
    lan = {"192.168.1.209": FakeTerminal(SERIAL)}
    zk = types.ModuleType("zk")
    zk.ZK = lambda ip, **kwargs: lan[ip]
    monkeypatch.setitem(sys.modules, "zk", zk)
    runner = importlib.import_module("essl_agent.runner")
    monkeypatch.setattr(runner, "tcp_reachable", lambda ip, port, timeout=3: ip in lan)
    monkeypatch.setattr(importlib.import_module("essl_agent.discovery"), "local_ipv4", lambda: "192.168.1.5")
    return types.SimpleNamespace(runner=runner, config=importlib.import_module("essl_agent.config"), lan=lan)


def test_the_agent_and_the_server_speak_the_same_protocol(live_server, essl_agent, sink, tmp_path):
    agent = AgentFactory()
    device = DeviceFactory(agent=agent, office=agent.office, serial_number=None, expected_serial=SERIAL, ip_address="192.168.1.209", identity_status=Device.IdentityStatus.UNVERIFIED)
    cfg = essl_agent.config.AgentConfig(server_url=live_server.url, token=agent.token, queue_path=str(tmp_path / "queue.sqlite3"))
    office_agent = essl_agent.runner.Agent(cfg)

    config = office_agent.heartbeat()
    assert config["agent"]["code"] == agent.code and [dev.uid for dev in cfg.devices] == [str(device.uid)]

    summary = office_agent.run_once()
    assert summary["announced"] == 1 and summary["users"] == 1 and summary["uploaded"] == 3 and office_agent.store.pending_count() == 0
    device.refresh_from_db()
    assert device.serial_number == SERIAL and device.identity_status == Device.IdentityStatus.VERIFIED and device.mac_address == "00:17:61:12:9c:49"
    assert DeviceUser.objects.get(device=device).pin == "1" and len(sink.of(device)) == 3
    assert set(SyncLog.objects.filter(device=device).values_list("sync_type", flat=True)) == {"USERS", "ATTENDANCE"}

    office_agent.heartbeat()
    device.refresh_from_db()
    assert device.last_seen_at is not None and device.clock_offset_seconds is not None

    assert office_agent.run_once()["requests"] == 0  # nothing new: the agent says nothing

    essl_agent.lan["192.168.1.209"] = FakeTerminal("NCD0000000999")
    essl_agent.lan["192.168.1.209"].logs.append(types.SimpleNamespace(uid=4, user_id="1", timestamp=datetime(2026, 9, 22, 10, 0), status=15, punch=0))
    office_agent.run_once()
    device.refresh_from_db()
    assert device.identity_status == Device.IdentityStatus.IDENTITY_MISMATCH and "NCD0000000999" in device.identity_message
    assert len(sink.of(device)) == 3 and cfg.devices[0].identity_blocked
    office_agent.store.close()
