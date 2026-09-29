"""Replay of the ADMS exchanges captured from the eSSL receiver (devices/tests/legacy/essl_adms_exchanges.json).

The captured sequence (a private eSSL server, one terminal registered for push, one unknown serial, one request without
a serial) is replayed against ``/iclock/<device_token>/…``. Every reply must be byte-identical to eSSL's, including the
status, the content type and the Content-Length, except the documented, intended differences (docs/decisions/devices.md):

* the option block advertises ``TransFlag=AttLog OpLog`` only (eSSL asked for photos, templates and user pictures);
* a terminal whose serial and token do not belong together, or that states no serial, is answered ``OK`` and
  quarantined instead of being handed the option block (eSSL answered any serial, spec §I.21).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.test import Client

from devices.models import AdmsRequest, AdmsUnknownDevice, DeviceUser
from devices.services import adms
from devices.services.devices import enable_adms
from devices.tests.factories import DeviceFactory

pytestmark = pytest.mark.django_db
EXCHANGES = json.loads((Path(__file__).parent / "legacy" / "essl_adms_exchanges.json").read_text())
REGISTERED = "NCD8252101398"
LEGACY_TRANSFLAG = "TransFlag=TransData AttLog OpLog AttPhoto EnrollUser ChgUser EnrollFP ChgFP UserPic\r\n"


def expected(exchange: dict) -> str:
    """eSSL's reply, with the documented differences applied."""
    body = exchange["response"]["body"]
    serial = exchange["request"]["query"].get("SN")
    if body.startswith("GET OPTION FROM:"):
        if serial != REGISTERED:
            return "OK"
        return body.replace(LEGACY_TRANSFLAG, "TransFlag=AttLog OpLog\r\n")
    return body


def replay(token: str, exchange: dict):
    request = exchange["request"]
    endpoint = request["path"].removeprefix("/iclock/")
    query = "&".join(f"{key}={value}" for key, value in request["query"].items())
    url = f"/iclock/{token}/{endpoint}?{query}"
    client = Client()
    if request["method"] == "GET":
        return client.get(url)
    return client.post(url, data=(request["body"] or "").encode(), content_type="text/plain")


def test_the_capture_covers_the_receiver():
    kinds = {adms.classify(exchange["request"]["method"], exchange["request"]["path"].removeprefix("/iclock/"), exchange["request"]["query"])[0] for exchange in EXCHANGES}
    assert {"HANDSHAKE", "ATTLOG", "OPERLOG", "GETREQUEST", "DEVICECMD", "PING", "REGISTRY"} <= kinds


def test_replies_match_essl_except_the_documented_differences(adms_on, sink):
    device = DeviceFactory(serial_number=None, expected_serial=REGISTERED, ip_address=None)
    device, token = enable_adms(device, user=None)
    differences = []
    for index, exchange in enumerate(EXCHANGES):
        response = replay(token, exchange)
        legacy = exchange["response"]
        want = expected(exchange)
        assert response.status_code == legacy["status"] == 200, index
        assert response["Content-Type"] == legacy["content_type"], index
        assert response.content.decode() == want, (index, response.content, want)
        assert response["Content-Length"] == str(len(want.encode())), index
        if want != legacy["body"]:
            differences.append(index)
    assert differences == [0, 8, 10]  # the handshake TransFlag; the unknown serial and the no-serial handshakes
    # What the replay left behind: one evidence row per request, the punches once, the user from OPERLOG, the quarantine.
    assert AdmsRequest.objects.count() == len(EXCHANGES)
    assert sorted((punch.pin, str(punch.device_time)) for punch in sink.of(device)) == [("7", "2026-09-22 09:31:05"), ("8", "2026-09-22 09:40:44")]
    assert DeviceUser.objects.get(device=device, pin="7").name == "Ravi"
    assert list(AdmsUnknownDevice.objects.values_list("serial_number", "request_count", "last_reason")) == [("ZZZ0000000001", 2, "TOKEN_MISMATCH")]  # replayed on the registered token
    device.refresh_from_db()
    assert device.serial_number == REGISTERED and device.attendance_count == 2
