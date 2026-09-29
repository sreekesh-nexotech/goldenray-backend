"""Test doubles for the office agent: a fake ``zk`` (pyzk) module with in-memory terminals, a fake platform, a clock.

No socket is opened and no real terminal or server is contacted. The doubles answer with a serial and a MAC, hold
users and punches, and accept or refuse uploads — which is all the agent's rules depend on.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime

import pytest
from essl_agent import discovery, runner
from essl_agent.config import AgentConfig, DeviceConfig
from essl_agent.store import Store
from essl_agent.uploader import Refused

FORBIDDEN = {
    "delete_user",
    "set_user",
    "clear_attendance",
    "delete_attendance",
    "clear_data",
    "set_time",
    "disable_device",
    "enable_device",
    "restart",
    "poweroff",
    "unlock",
    "test_voice",
    "write_lcd",
    "clear_lcd",
}


class User:
    def __init__(self, uid, user_id, name="", privilege=0, password="", group_id="1", card=0):
        self.uid, self.user_id, self.name, self.privilege, self.password, self.group_id, self.card = uid, user_id, name, privilege, password, group_id, card


class Log:
    def __init__(self, uid, user_id, timestamp, status=15, punch=0):
        self.uid, self.user_id, self.timestamp, self.status, self.punch = uid, user_id, timestamp, status, punch


class Terminal:
    def __init__(self, serial, mac="00:17:61:aa:aa:01", name="x 2008"):
        self.serial, self.mac, self.name = serial, mac, name
        self.users: list[User] = []
        self.logs: list[Log] = []
        self.online = True
        self.calls: list[str] = []
        self.clock = datetime(2026, 9, 22, 9, 0, 0)

    def punch(self, uid, user_id, when, status=15, punch=0):
        self.logs.append(Log(uid, user_id, when, status, punch))


class Connection:
    def __init__(self, terminal: Terminal):
        self.terminal = terminal

    def _call(self, name):
        if name in FORBIDDEN:
            raise AssertionError(f"{name} must never be called")
        self.terminal.calls.append(name)

    def get_device_name(self):
        self._call("get_device_name")
        return self.terminal.name

    def get_serialnumber(self):
        self._call("get_serialnumber")
        return self.terminal.serial

    def get_mac(self):
        self._call("get_mac")
        return self.terminal.mac

    def get_firmware_version(self):
        self._call("get_firmware_version")
        return "Ver 6.60 Aug 19 2021"

    def get_platform(self):
        self._call("get_platform")
        return "ZAM180_TFT"

    def get_face_version(self):
        self._call("get_face_version")
        raise RuntimeError("no face module answer")

    def get_fp_version(self):
        self._call("get_fp_version")
        return 10

    def get_time(self):
        self._call("get_time")
        return self.terminal.clock

    def get_network_params(self):
        self._call("get_network_params")
        return {"ip": "192.168.1.209"}

    def get_users(self):
        self._call("get_users")
        return list(self.terminal.users)

    def get_attendance(self):
        self._call("get_attendance")
        return list(self.terminal.logs)

    def disconnect(self):
        self._call("disconnect")


class Terminals(dict):
    """``(ip, port) -> Terminal``: what answers at each address on the fake LAN."""

    def place(self, ip, terminal, port=4370):
        self[(ip, port)] = terminal
        return terminal

    def reachable(self, ip, port, timeout=3):
        terminal = self.get((ip, port))
        return terminal is not None and terminal.online


@pytest.fixture
def lan(monkeypatch):
    terminals = Terminals()

    class ZK:
        def __init__(self, ip, port=4370, timeout=15, password=0, force_udp=False, ommit_ping=False):
            self.address = (ip, port)

        def connect(self):
            terminal = terminals.get(self.address)
            if terminal is None or not terminal.online:
                raise ConnectionError(f"can't reach device {self.address}")
            terminal.calls.append("connect")
            return Connection(terminal)

    module = types.ModuleType("zk")
    module.ZK = ZK
    monkeypatch.setitem(sys.modules, "zk", module)
    monkeypatch.setattr(runner, "tcp_reachable", terminals.reachable)
    monkeypatch.setattr(discovery, "local_ipv4", lambda: "192.168.1.5")
    monkeypatch.setattr(discovery, "default_gateway", lambda: "192.168.1.1")
    monkeypatch.setattr(discovery, "scan_port", lambda hosts, port=4370, timeout=0.4, workers=64: [host for host in hosts if terminals.reachable(host, port)])
    return terminals


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Platform:
    """The agent protocol as the platform answers it (enough of it for the agent's rules)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.devices: dict[str, str] = {}  # serial -> device uid
        self.refuse: dict[str, Refused] = {}  # serial -> refusal of announce
        self.refuse_uploads: dict[str, Refused] = {}  # serial -> refusal of the user and attendance uploads
        self.failure: Exception | None = None  # raised by every call while set
        self.stored: dict[tuple, dict] = {}
        self.replayed: dict[str, dict] = {}
        self.config = {
            "agent": {"code": "OFFICE-001-AGENT", "office": {"name": "Main Office"}},
            "intervals": {"heartbeat_seconds": 60, "sync_seconds": 300},
            "announce_interval_seconds": 3600,
            "upload_batch_size": 200,
            "devices": [],
        }

    def _enter(self, name, payload):
        self.calls.append((name, payload))
        if self.failure is not None:
            raise self.failure

    def names(self):
        return [name for name, _ in self.calls]

    def heartbeat(self, payload):
        self._enter("heartbeat", payload)
        return self.config

    def announce_device(self, payload):
        self._enter("announce", payload)
        serial = payload["serial_number"]
        if serial in self.refuse:
            raise self.refuse[serial]
        uid = self.devices.setdefault(serial, f"00000000-0000-0000-0000-{len(self.devices) + 1:012d}")
        return {"device": uid, "name": payload["name"], "serial_number": serial, "created": False}

    def report_identity_mismatch(self, payload):
        self._enter("identity_mismatch", payload)
        return {"recorded": True}

    def report_discovery(self, payload):
        self._enter("discovery", payload)
        return {"recorded": True, "matches": [], "unmatched": payload["found"], "still_awaiting": []}

    def upload_users(self, device, serial, users, *, read_at, idempotency_key):
        self._enter("users", {"device": device, "serial_number": serial, "users": users, "read_at": read_at, "key": idempotency_key})
        if serial in self.refuse_uploads:
            raise self.refuse_uploads[serial]
        return {"device": device, "received": len(users), "created": len(users), "updated": 0, "invalid": 0}

    def upload_attendance(self, device, serial, records, *, batch_id, idempotency_key):
        self._enter("attendance", {"device": device, "serial_number": serial, "records": records, "batch_id": batch_id, "key": idempotency_key})
        if serial in self.refuse_uploads:
            raise self.refuse_uploads[serial]
        if idempotency_key in self.replayed:
            return self.replayed[idempotency_key]
        new = duplicate = 0
        for record in records:
            key = (serial, record["pin"], record["device_time"], record["status"], record["punch"])
            if key in self.stored:
                duplicate += 1
            else:
                self.stored[key] = record
                new += 1
        answer = {"device": device, "received": len(records), "new": new, "duplicate": duplicate, "invalid": 0, "discarded": 0}
        self.replayed[idempotency_key] = answer
        return answer


@pytest.fixture
def platform():
    return Platform()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_agent(tmp_path, platform, clock, lan):
    stores = []

    def build(*devices: DeviceConfig, store: Store | None = None, **settings):
        cfg = AgentConfig(server_url="https://api.flarize.test", token="fl_abc_secret", agent_code="OFFICE-001-AGENT", queue_path=str(tmp_path / "queue.sqlite3"), **settings)
        cfg.devices = list(devices)
        store = store or Store(cfg.queue_path)
        stores.append(store)
        return runner.Agent(cfg, store=store, uploader=platform, clock=clock)

    yield build
    for store in stores:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - already closed by the test
            pass


def mars(**overrides) -> DeviceConfig:
    values = {"name": "MARS-01", "ip": "192.168.1.209", "expected_serial": "NCD8253601138"}
    values.update(overrides)
    return DeviceConfig(**values)
