"""/iclock/<device_token>/… — the ADMS receiver: flag, identity (serial AND token), admission, parsing, evidence, replies."""

from __future__ import annotations

import pytest
from django.test import Client

from devices.models import AdmsRequest, AdmsUnknownDevice, Device, DeviceUser, SyncLog
from devices.models.adms import MAX_STORED_BODY_BYTES
from devices.services import adms, ingest
from devices.services.devices import enable_adms
from devices.tests.conftest import events
from devices.tests.factories import DeviceFactory, DeviceUserFactory
from hr.tests.factories import EmployeeFactory

pytestmark = pytest.mark.django_db
SERIAL = "NCD8252101398"
ATTLOG = "7\t2026-09-22 09:31:05\t0\t15\t0\t0\n8\t2026-09-22 09:40:44\t0\t1\t\t0\nnot a punch line\n"


def iclock(token, endpoint="cdata", query="", body=None, method="post", **extra):
    client = Client()
    url = f"/iclock/{token}/{endpoint}" + (f"?{query}" if query else "")
    if method == "get":
        return client.get(url, **extra)
    return getattr(client, method)(url, data=body if body is not None else b"", content_type="text/plain", **extra)


def text(response) -> str:
    return response.content.decode()


def last_request() -> AdmsRequest:
    return AdmsRequest.objects.order_by("-id").first()


@pytest.fixture
def pushed(adms_on):
    """A terminal registered with its serial and push enabled: ``(device, token)``."""
    device = DeviceFactory(serial_number=SERIAL, expected_serial=SERIAL, ip_address=None)
    device, token = enable_adms(device, user=None)
    return device, token


class TestGate:
    def test_flag_off_is_404(self, db):
        device = DeviceFactory(serial_number=SERIAL)
        _, token = enable_adms(device, user=None)
        assert iclock(token, query=f"SN={SERIAL}&options=all", method="get").status_code == 404
        assert not AdmsRequest.objects.exists()

    def test_other_methods_are_acknowledged_and_not_recorded(self, pushed):
        _, token = pushed
        response = iclock(token, query=f"SN={SERIAL}", method="patch")
        assert response.status_code == 200 and text(response) == "OK" and not AdmsRequest.objects.exists()


class TestHandshake:
    def test_option_block_plain_text_with_content_length(self, pushed):
        device, token = pushed
        response = iclock(token, query=f"SN={SERIAL}&options=all&pushver=2.4.1&language=69", method="get")
        body = text(response)
        assert response.status_code == 200 and response["Content-Type"] == "text/plain; charset=utf-8" and int(response["Content-Length"]) == len(response.content)
        assert body == adms.handshake_options(SERIAL) and body.startswith(f"GET OPTION FROM: {SERIAL}\r\n")
        assert "TransFlag=AttLog OpLog\r\n" in body and "TimeZone" not in body and "AttPhoto" not in body and "UserPic" not in body
        device.refresh_from_db()
        assert device.adms_last_handshake_at and device.adms_registration_state == Device.RegistrationState.REGISTERED and device.adms_request_count == 1
        assert device.adms_options["query"]["pushver"] == "2.4.1"
        row = last_request()
        assert row.request_kind == "HANDSHAKE" and row.device_id == device.pk and row.path == "/iclock/[redacted]/cdata" and token not in row.path
        assert row.response_body == body and row.device_serial == SERIAL and row.query["options"] == "all"

    def test_a_label_registered_terminal_adopts_the_serial_it_states(self, adms_on):
        device = DeviceFactory(serial_number=None, expected_serial=SERIAL, identity_status=Device.IdentityStatus.UNVERIFIED)
        device, token = enable_adms(device, user=None)
        iclock(token, query=f"SN={SERIAL}&options=all", method="get")
        device.refresh_from_db()
        assert device.serial_number == SERIAL and device.identity_status == Device.IdentityStatus.VERIFIED and "over ADMS" in device.identity_message

    def test_aspx_and_trailing_slash_variants(self, pushed):
        device, token = pushed
        assert text(iclock(token, endpoint="cdata.aspx", query=f"SN={SERIAL}&options=all", method="get")).startswith("GET OPTION FROM:")
        assert text(iclock(token, endpoint="getrequest/", query=f"SN={SERIAL}", method="get")) == "OK"
        device.refresh_from_db()
        assert device.adms_last_command_poll_at is not None


class TestIdentity:
    def test_a_registered_serial_with_the_wrong_token_is_quarantined(self, pushed, sink):
        device, _ = pushed
        response = iclock("not-the-token", query=f"SN={SERIAL}&table=ATTLOG", body=ATTLOG)
        assert text(response) == "OK" and not sink.punches
        entry = AdmsUnknownDevice.objects.get(serial_number=SERIAL)
        assert entry.last_reason == AdmsUnknownDevice.Reason.TOKEN_MISMATCH and entry.request_count == 1 and entry.last_path == "/iclock/[redacted]/cdata"
        assert "7\t2026-09-22" in entry.last_body_excerpt
        row = last_request()
        assert row.device_id is None and "Quarantined" in row.parse_error
        device.refresh_from_db()
        assert device.adms_last_seen_at is None  # not contact from the terminal

    def test_the_token_of_one_device_with_another_serial(self, pushed, sink):
        _, token = pushed
        iclock(token, query="SN=NCD0000000999&table=ATTLOG", body=ATTLOG)
        iclock(token, query="SN=NCD0000000999&table=ATTLOG", body=ATTLOG)
        entry = AdmsUnknownDevice.objects.get(serial_number="NCD0000000999")
        assert entry.last_reason == AdmsUnknownDevice.Reason.TOKEN_MISMATCH and entry.request_count == 2 and not sink.punches

    def test_unknown_token_and_unknown_serial(self, adms_on):
        response = iclock("x" * 200, query="SN=ZZZ0000000001&options=all", method="get")
        assert text(response) == "OK"  # an unknown terminal is not given the option block
        assert AdmsUnknownDevice.objects.get(serial_number="ZZZ0000000001").last_reason == AdmsUnknownDevice.Reason.UNKNOWN_SERIAL

    def test_no_serial_is_evidence_only(self, pushed):
        _, token = pushed
        assert text(iclock(token, query="options=all", method="get")) == "OK"
        row = last_request()
        assert row.device_id is None and row.device_serial == "" and "No serial" in row.parse_error and not AdmsUnknownDevice.objects.exists()


class TestAdmission:
    @pytest.mark.parametrize(
        "change, message",
        [({"is_active": False}, "deactivated"), ({"adms_enabled": False}, "not enabled"), ({"adms_allowed_ips": ["10.0.0.0/8"]}, "allow-list")],
    )
    def test_refused_pushes_are_evidence_only(self, pushed, sink, change, message):
        device, token = pushed
        Device.all_objects.filter(pk=device.pk).update(**change)
        response = iclock(token, query=f"SN={SERIAL}&table=ATTLOG", body=ATTLOG)
        assert text(response) == "OK" and not sink.punches
        row = last_request()
        assert row.device_id == device.pk and message in row.parse_error and row.records_new == 0
        device.refresh_from_db()
        assert device.adms_last_seen_at is None and device.attendance_count == 0

    def test_an_allowed_address_is_admitted(self, pushed, sink):
        device, token = pushed
        Device.all_objects.filter(pk=device.pk).update(adms_allowed_ips=["127.0.0.0/8"])
        assert text(iclock(token, query=f"SN={SERIAL}&table=ATTLOG", body=ATTLOG)) == "OK: 2" and len(sink.punches) == 2
        device.refresh_from_db()
        assert device.adms_source_ip == "127.0.0.1" and device.adms_last_push_at is not None


class TestAttlog:
    def test_punches_go_through_the_sink_once_and_are_announced(self, pushed, sink):
        device, token = pushed
        employee = EmployeeFactory(office=device.office)
        DeviceUserFactory(device=device, pin="7", employee=employee)
        response = iclock(token, query=f"SN={SERIAL}&table=ATTLOG&Stamp=9999", body=ATTLOG)
        assert text(response) == "OK: 2" and int(response["Content-Length"]) == 5
        punches = sorted(sink.of(device), key=lambda punch: punch.pin)
        assert [(punch.pin, punch.status_code, punch.punch_code, punch.source) for punch in punches] == [("7", 15, 0, "ADMS_PUSH"), ("8", 1, 0, "ADMS_PUSH")]
        assert punches[0].raw_payload["_line"].startswith("7\t") and punches[0].adms_request_id == last_request().id
        row = last_request()
        assert (row.request_kind, row.records_parsed, row.records_new, row.records_duplicate, row.records_invalid) == ("ATTLOG", 2, 2, 0, 1)
        [event] = events(ingest.EVENT)
        assert event["source"] == "ADMS_PUSH" and event["employee_uids"] == [str(employee.uid)] and event["unmapped_pins"] == ["8"] and event["dates"] == ["2026-09-22"]
        device.refresh_from_db()
        assert device.attendance_count == 2 and device.last_punch_at is not None
        assert SyncLog.objects.get(device=device, sync_type=SyncLog.Type.ATTENDANCE).details["transport"] == "ADMS_PUSH"

        again = iclock(token, query=f"SN={SERIAL}&table=ATTLOG&Stamp=9999", body=ATTLOG)
        assert text(again) == "OK: 2" and len(sink.punches) == 2 and len(events(ingest.EVENT)) == 1
        assert (last_request().records_new, last_request().records_duplicate) == (0, 2)

    def test_the_same_punch_from_the_agent_is_a_duplicate(self, pushed, sink):
        device, token = pushed
        iclock(token, query=f"SN={SERIAL}&table=ATTLOG", body=ATTLOG)
        counts = ingest.ingest(device, [{"pin": "7", "device_time": "2026-09-22 09:31:05", "status": 15, "punch": 0}], source=ingest.AGENT_PUSH)
        assert counts["new"] == 0 and counts["duplicate"] == 1

    def test_without_a_punch_store_nothing_is_kept_and_nothing_announced(self, pushed):
        _, token = pushed
        assert text(iclock(token, query=f"SN={SERIAL}&table=ATTLOG", body=ATTLOG)) == "OK: 2"
        assert last_request().extra["discarded"] == 2 and events(ingest.EVENT) == []

    def test_a_store_failure_is_answered_error_and_kept_as_evidence(self, pushed, sink, monkeypatch):
        device, token = pushed

        def boom(punches):
            raise RuntimeError("store down")

        monkeypatch.setattr(sink, "store", boom)
        response = iclock(token, query=f"SN={SERIAL}&table=ATTLOG", body=ATTLOG)
        assert response.status_code == 200 and text(response) == "ERROR"
        row = last_request()
        assert row.response_body == "ERROR" and "store down" in row.parse_error and bytes(row.body).startswith(b"7\t2026")
        device.refresh_from_db()
        assert device.attendance_count == 0 and device.adms_last_push_at is not None  # it did reach us
        assert not SyncLog.objects.filter(device=device).exists() and events(ingest.EVENT) == []

    def test_malformed_and_implausible_lines_are_counted_invalid(self, pushed, sink):
        _, token = pushed
        body = "\t2026-09-22 09:00:00\t0\t1\n9\tyesterday\t0\t1\n9\t1999-01-01 00:00:00\t0\t1\n9  2026-09-22 10:00:00  0  1\n\x00\n"
        assert text(iclock(token, query=f"SN={SERIAL}&table=ATTLOG", body=body)) == "OK: 2"
        row = last_request()
        assert (row.records_parsed, row.records_new, row.records_invalid) == (2, 1, 3) and "\x00" not in row.body_text


class TestUsers:
    def test_userinfo_upserts_device_users_and_redacts_secrets(self, pushed):
        device, token = pushed
        body = "PIN=11\tName=Asha\tPri=14\tPasswd=9999\tCard=123\tGrp=1\nPIN=12\tName=Ravi\tPri=0\tPasswd=\tTMP=AAAABBBB\n"
        assert text(iclock(token, query=f"SN={SERIAL}&table=USERINFO", body=body)) == "OK: 2"
        users = {user.pin: user for user in DeviceUser.objects.filter(device=device)}
        assert set(users) == {"11", "12"} and users["11"].privilege == 14 and users["11"].has_password and not users["12"].has_password
        assert users["11"].raw_payload["Passwd"] == "***" and users["11"].device_uid is None
        row = last_request()
        stored = bytes(row.body).decode()
        assert "9999" not in stored and "AAAABBBB" not in stored and "Passwd=***" in stored and "TMP=***" in stored and "9999" not in row.body_text
        assert row.extra["users_applied"] == 2 and row.extra["redacted"] and row.extra["parsed"][0]["Passwd"] == "***"
        assert not SyncLog.objects.filter(device=device, sync_type=SyncLog.Type.USERS).exists()  # a push is not a whole-table read

    def test_operlog_user_lines_only(self, pushed):
        device, token = pushed
        body = "USER PIN=7\tName=Ravi\tPri=0\tPasswd=4321\tCard=[000]\tGrp=1\tTZ=0000000100000000\tVerify=0\nOPLOG 4\t0\t2026-09-22 09:35:00\t0\t0\t0\t0\n"
        assert text(iclock(token, query=f"SN={SERIAL}&table=OPERLOG&OpStamp=9999", body=body)) == "OK: 2"
        user = DeviceUser.objects.get(device=device)
        assert (user.pin, user.name, user.has_password) == ("7", "Ravi", True) and "4321" not in bytes(last_request().body).decode()

    def test_a_push_changes_only_what_it_carries(self, pushed):
        """A USER line without Card/Grp/Passwd (and never with the terminal's internal uid) leaves what an agent read."""
        device, token = pushed
        DeviceUserFactory(device=device, pin="7", name="Ravi", device_uid=42, card="999", group_id="3", privilege=0, has_password=True)
        assert text(iclock(token, query=f"SN={SERIAL}&table=OPERLOG", body="USER PIN=7\tName=Ravi K\tPri=14\n")) == "OK: 1"
        user = DeviceUser.objects.get(device=device, pin="7")
        assert (user.name, user.privilege, user.device_uid, user.card, user.group_id, user.has_password) == ("Ravi K", 14, 42, "999", "3", True)

    def test_an_out_of_range_privilege_is_not_a_poison_push(self, pushed):
        """Pri beyond a smallint used to fail the insert: ERROR, and the terminal resent the same push forever."""
        device, token = pushed
        assert text(iclock(token, query=f"SN={SERIAL}&table=USERINFO", body="PIN=11\tName=Asha\tPri=99999\n")) == "OK: 1"
        user = DeviceUser.objects.get(device=device, pin="11")
        assert user.privilege is None and user.raw_payload["Pri"] == "99999" and last_request().parse_error == ""

    def test_out_of_range_codes_are_invalid_punches_not_a_poison_push(self, pushed, sink):
        _, token = pushed
        body = "7\t2026-09-22 09:31:05\t99999\t15\t0\n8\t2026-09-22 09:40:44\t0\t1\t0\n"
        assert text(iclock(token, query=f"SN={SERIAL}&table=ATTLOG", body=body)) == "OK: 2"
        assert [punch.pin for punch in sink.punches.values()] == ["8"] and last_request().records_invalid == 1


class TestEvidence:
    def test_biometric_bodies_are_never_stored(self, pushed):
        _, token = pushed
        for table in ("ATTPHOTO", "BIODATA"):
            assert text(iclock(token, query=f"SN={SERIAL}&table={table}", body=b"\xff\xd8" + b"photo" * 100)) == "OK"
            row = last_request()
            assert row.request_kind == table and row.body is None and row.body_text == "" and row.body_bytes == 502

    @pytest.mark.parametrize("table", ["ATTPHOTO", "BIODATA"])
    def test_biometric_bodies_are_never_stored_when_quarantined_or_refused(self, pushed, table):
        device, token = pushed
        template = b"PIN=7\tFID=0\tTMP=SECRETTEMPLATE" * 20
        iclock("not-the-token", query=f"SN=ZZZ0000000042&table={table}", body=template)  # unknown serial: quarantined
        iclock(token, query=f"SN=NCD0000000999&table={table}", body=template)  # the token with another serial: quarantined
        Device.all_objects.filter(pk=device.pk).update(is_active=False)
        iclock(token, query=f"SN={SERIAL}&table={table}", body=template)  # refused
        rows = list(AdmsRequest.objects.order_by("id"))
        assert len(rows) == 3 and all(row.request_kind == table and row.body is None and row.body_text == "" and row.body_bytes == len(template) for row in rows)
        assert not any("SECRETTEMPLATE" in entry.last_body_excerpt for entry in AdmsUnknownDevice.objects.all())

    def test_the_stored_body_is_capped_at_one_mebibyte(self, pushed):
        _, token = pushed
        payload = b"x" * (MAX_STORED_BODY_BYTES + 10)
        assert text(iclock(token, query=f"SN={SERIAL}&table=FIRSTSEEN", body=payload)) == "OK"
        row = last_request()
        assert row.request_kind == "CDATA" and row.table_name == "FIRSTSEEN" and row.body_bytes == MAX_STORED_BODY_BYTES + 10
        assert len(row.body) == MAX_STORED_BODY_BYTES and row.body_truncated and len(row.body_text) == 4000

    def test_credentials_headers_are_dropped(self, pushed):
        _, token = pushed
        iclock(token, endpoint="ping", query=f"SN={SERIAL}", method="get", HTTP_AUTHORIZATION="Bearer secret", HTTP_USER_AGENT="iClock Proxy/1.09")
        headers = {key.lower(): value for key, value in last_request().headers.items()}
        assert "authorization" not in headers and headers["user-agent"] == "iClock Proxy/1.09"

    def test_command_endpoints_and_the_catch_all(self, pushed):
        device, token = pushed
        assert text(iclock(token, endpoint="devicecmd", query=f"SN={SERIAL}", body="ID=1&Return=0&CMD=INFO\n")) == "OK"
        assert "issues no commands" in last_request().extra["note"]
        assert text(iclock(token, endpoint="registry", query=f"SN={SERIAL}", method="get")) == "OK" and last_request().request_kind == "REGISTRY"
        assert text(iclock(token, endpoint="something/else", query=f"SN={SERIAL}", method="get")) == "OK" and last_request().request_kind == "UNKNOWN"
        device.refresh_from_db()
        assert device.adms_request_count == 3

    def test_a_gb18030_body_is_decoded(self, pushed):
        _, token = pushed
        iclock(token, query=f"SN={SERIAL}&table=USERINFO", body="PIN=21\tName=王伟\n".encode("gb18030"))
        assert last_request().body_encoding == "gb18030" and DeviceUser.objects.get(pin="21").name == "王伟"

    def test_a_request_that_cannot_be_recorded_is_answered_error(self, pushed, monkeypatch):
        _, token = pushed

        def fail(*args, **kwargs):
            raise ValueError("disk full")

        monkeypatch.setattr(adms, "_store", fail)
        assert text(iclock(token, query=f"SN={SERIAL}&options=all", method="get")) == "ERROR"
        row = last_request()
        assert row.request_kind == AdmsRequest.Kind.UNSTORABLE and row.response_body == "ERROR" and "ValueError" in row.parse_error


class TestThrottle:
    def test_per_token_budget(self, pushed, monkeypatch):
        from devices.views import iclock as view

        _, token = pushed
        monkeypatch.setattr(view, "parse_rate", lambda rate: (2, 60))
        replies = [text(iclock(token, endpoint="ping", query=f"SN={SERIAL}", method="get")) for _ in range(3)]
        assert replies == ["OK", "OK", "ERROR"] and AdmsRequest.objects.count() == 2
        assert text(iclock("another-token", endpoint="ping", query=f"SN={SERIAL}", method="get")) == "OK"  # budgets are per token
