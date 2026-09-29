"""The read-only terminal client (pyzk mocked) and the HTTPS client (a local HTTP stub)."""

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from essl_agent import zk_reader
from essl_agent.uploader import AuthRejected, Refused, ServerUnavailable, Uploader

from .conftest import Terminal, User


class TestZkReader:
    def test_reads_are_verbatim_and_read_only(self, lan):
        terminal = lan.place("192.168.1.209", Terminal("NCD1"))
        terminal.users = [User(1, 55, " Asha ", privilege=14, password="9999", card=123)]
        terminal.punch(7, 55, datetime(2026, 9, 22, 9, 31, 5), status=15, punch=0)
        info = zk_reader.read_info("192.168.1.209")
        assert info["serial_number"] == "NCD1" and info["device_time"] == "2026-09-22T09:00:00" and "face_version" in info["unavailable"]
        [user] = zk_reader.read_users("192.168.1.209")
        assert (user["pin"], user["name"], user["has_password"], user["card"], user["device_uid"]) == ("55", "Asha", True, "123", 1)
        assert "password" not in user["raw_payload"]  # the terminal user's password never leaves the office
        [record] = zk_reader.read_attendance("192.168.1.209")
        assert record == {"device_record_uid": 7, "pin": "55", "device_time": "2026-09-22T09:31:05", "status": 15, "punch": 0, "raw_payload": record["raw_payload"]}
        assert set(terminal.calls) <= zk_reader.READ_CALLS

    def test_an_unreachable_terminal_is_a_device_error(self, lan):
        with pytest.raises(zk_reader.DeviceError, match="ConnectionError"):
            zk_reader.read_info("192.168.1.250")

    def test_jsonable(self):
        assert zk_reader.jsonable({"b": b"\xc4\xe3", "d": datetime(2026, 1, 1), "l": (1, 2)}) == {"b": "你", "d": "2026-01-01T00:00:00", "l": [1, 2]}
        assert zk_reader.jsonable(object()).startswith("<object")

    def test_tcp_reachable_is_false_for_a_closed_port(self):
        assert zk_reader.tcp_reachable("127.0.0.1", 9, timeout=0.2) is False


class Stub(BaseHTTPRequestHandler):
    answers: dict = {}
    seen: list = []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        Stub.seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        status, payload = Stub.answers.get(self.path.split("?")[0], (200, {"ok": True}))
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Stub.answers, Stub.seen = {}, []
    httpd = HTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/api/agent/v1"
    httpd.shutdown()
    httpd.server_close()


class TestUploader:
    def test_paths_headers_and_payloads(self, server):
        client = Uploader(server, "fl_abc_secret", timeout=5)
        client.get_config()
        client.upload_attendance("u1", "NCD1", [{"pin": "7"}], batch_id="b1", idempotency_key="att-b1")
        client.sync_status(device="u1")
        client.upload_users(None, "NCD1", [], read_at="2026-09-22T09:00:00+00:00", idempotency_key="usr-1")
        for call in (client.heartbeat, client.announce_device, client.report_identity_mismatch, client.report_discovery):
            call({"x": 1})
        paths = [entry["path"] for entry in Stub.seen]
        assert paths == [
            "/api/agent/v1/config/",
            "/api/agent/v1/sync/attendance/",
            "/api/agent/v1/sync-status/?device=u1",
            "/api/agent/v1/sync/users/",
            "/api/agent/v1/heartbeat/",
            "/api/agent/v1/devices/announce/",
            "/api/agent/v1/devices/identity-mismatch/",
            "/api/agent/v1/devices/discovery/",
        ]
        upload = Stub.seen[1]
        assert upload["headers"]["Authorization"] == "Bearer fl_abc_secret" and upload["headers"]["Idempotency-Key"] == "att-b1"
        assert upload["body"] == {"device": "u1", "serial_number": "NCD1", "batch_id": "b1", "records": [{"pin": "7"}]}

    @pytest.mark.parametrize(
        "status, payload, error, code",
        [
            (401, {"code": "not_authenticated"}, AuthRejected, None),
            (403, {"code": "permission_denied"}, AuthRejected, None),
            (409, {"code": "device_bound_elsewhere", "message": "bound to OFFICE-002-AGENT"}, Refused, "device_bound_elsewhere"),
            (404, {"code": "device_not_found", "message": "no"}, Refused, "device_not_found"),
            (409, {"code": "idempotency_in_progress", "message": "wait"}, ServerUnavailable, None),
            (429, {"code": "throttled"}, ServerUnavailable, None),
            (503, {"code": "down"}, ServerUnavailable, None),
        ],
    )
    def test_errors(self, server, status, payload, error, code):
        Stub.answers["/api/agent/v1/devices/announce/"] = (status, payload)
        with pytest.raises(error) as caught:
            Uploader(server, "fl_abc_secret", timeout=5).announce_device({})
        if code:
            assert caught.value.code == code and caught.value.status == status

    def test_network_failure_is_transient(self):
        with pytest.raises(ServerUnavailable):
            Uploader("http://127.0.0.1:9/api/agent/v1", "fl_x_y", timeout=1).get_config()
