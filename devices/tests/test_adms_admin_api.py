"""devices/adms/{status,requests,unknown-devices}/ — what the receiver is and what it has heard (module devices, view)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from devices.models import AdmsRequest
from devices.services import adms
from devices.services.devices import enable_adms
from devices.tests.factories import DeviceFactory
from devices.tests.test_iclock import ATTLOG, SERIAL, iclock

pytestmark = pytest.mark.django_db
BASE = "/api/v1/devices/adms/"


def evidence(**values) -> AdmsRequest:
    values.setdefault("received_at", timezone.now())
    return AdmsRequest.objects.create(id=adms._next_id(), method="GET", path="/iclock/[redacted]/cdata", **values)


class TestPermissions:
    @pytest.mark.parametrize("path", ["status/", "requests/", "unknown-devices/"])
    def test_anonymous_401_and_other_modules_403(self, api_client, auth_client, make_user, viewer_client, path):
        assert api_client.get(f"{BASE}{path}").status_code == 401
        assert auth_client(make_user(grants={"employees": ["view"]})).get(f"{BASE}{path}").status_code == 403
        assert viewer_client.get(f"{BASE}{path}").status_code == 200


class TestStatus:
    def test_what_the_receiver_is(self, viewer_client, adms_on):
        device, _ = enable_adms(DeviceFactory(serial_number=SERIAL, expected_serial=SERIAL), user=None)
        DeviceFactory()
        evidence(received_at=timezone.now() - timedelta(days=2))
        evidence()
        body = viewer_client.get(f"{BASE}status/").json()
        assert body["enabled"] is True and body["retention_days"] == 30 and body["online_seconds"] == 300 and body["offline_seconds"] == 900
        assert body["requests_last_24h"] == 1 and body["unknown_devices"] == 0 and body["missing_partitions"] == [] and body["devices_truncated"] is False
        assert [row["uid"] for row in body["devices"]] == [str(device.uid)] and body["device_path"] == "/iclock/<device token>/cdata"

    def test_flag_off_is_reported(self, viewer_client):
        assert viewer_client.get(f"{BASE}status/").json()["enabled"] is False


class TestRequests:
    def test_newest_first_filters_and_no_raw_bytes(self, viewer_client, pushed_device, sink):
        device, token = pushed_device
        iclock(token, query=f"SN={SERIAL}&options=all", method="get")
        iclock(token, query=f"SN={SERIAL}&table=ATTLOG", body=ATTLOG)
        iclock("wrong", query="SN=ZZZ1&options=all", method="get")
        rows = viewer_client.get(f"{BASE}requests/").json()["results"]
        assert [row["request_kind"] for row in rows] == ["HANDSHAKE", "ATTLOG", "HANDSHAKE"]
        assert "body" not in rows[1] and rows[1]["body_text"].startswith("7\t") and rows[1]["records_new"] == 2 and rows[1]["device"]["uid"] == str(device.uid)
        assert [row["device_serial"] for row in viewer_client.get(f"{BASE}requests/", {"serial": "ZZZ1"}).json()["results"]] == ["ZZZ1"]
        assert [row["request_kind"] for row in viewer_client.get(f"{BASE}requests/", {"kind": "ATTLOG"}).json()["results"]] == ["ATTLOG"]
        assert len(viewer_client.get(f"{BASE}requests/", {"device": str(device.uid)}).json()["results"]) == 2
        assert viewer_client.get(f"{BASE}requests/", {"kind": "NOPE"}).status_code == 400

    def test_cursor_pagination(self, viewer_client, django_assert_max_num_queries):
        device = DeviceFactory()
        for minute in range(5):
            evidence(received_at=timezone.now() - timedelta(minutes=minute), device=device)
        with django_assert_max_num_queries(5):
            first = viewer_client.get(f"{BASE}requests/", {"page_size": 2}).json()
        second = viewer_client.get(first["next"]).json()
        assert len(first["results"]) == 2 and len(second["results"]) == 2 and first["results"][0]["received_at"] > second["results"][0]["received_at"]


class TestUnknownDevices:
    def test_quarantine_list_and_filter(self, viewer_client, pushed_device):
        _, token = pushed_device
        iclock("wrong", query=f"SN={SERIAL}&options=all", method="get")
        iclock("wrong", query="SN=ZZZ2&options=all", method="get")
        iclock("wrong", query="SN=ZZZ2&options=all", method="get")
        rows = viewer_client.get(f"{BASE}unknown-devices/", {"ordering": "-request_count"}).json()["results"]
        assert [(row["serial_number"], row["request_count"], row["last_reason"]) for row in rows] == [("ZZZ2", 2, "UNKNOWN_SERIAL"), (SERIAL, 1, "TOKEN_MISMATCH")]
        assert [row["serial_number"] for row in viewer_client.get(f"{BASE}unknown-devices/", {"reason": "TOKEN_MISMATCH"}).json()["results"]] == [SERIAL]


@pytest.fixture
def pushed_device(adms_on):
    device = DeviceFactory(serial_number=SERIAL, expected_serial=SERIAL)
    return enable_adms(device, user=None)
