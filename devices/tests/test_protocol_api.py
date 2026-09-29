"""devices/protocol-mappings/ — CRUD with the live-scope uniqueness, and observed/ (codes the punch store has seen)."""

from datetime import datetime

import pytest

from audit.models import AuditLog
from devices.models import ProtocolMapping
from devices.services import ingest
from devices.tests.factories import DeviceFactory, ProtocolMappingFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/devices/protocol-mappings/"
FACE = {"field": "status", "raw_value": 15, "meaning_type": "VERIFY_MODE", "meaning_code": "face", "label": "Face", "confidence": "ASSUMED"}


def detail(mapping, suffix=""):
    return f"{URL}{mapping.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        mapping = ProtocolMappingFactory()
        for method, path in [("get", URL), ("post", URL), ("get", detail(mapping)), ("patch", detail(mapping)), ("delete", detail(mapping)), ("get", f"{URL}observed/")]:
            assert getattr(api_client, method)(path).status_code == 401

    def test_view_create_edit_manage(self, viewer_client, hr_client, auth_client, make_user):
        mapping = ProtocolMappingFactory()
        assert viewer_client.get(URL).status_code == 200 and hr_client.get(f"{URL}observed/").status_code == 200
        assert viewer_client.post(URL, FACE, format="json").status_code == 403
        assert viewer_client.patch(detail(mapping), {"label": "x"}, format="json").status_code == 403
        editor = auth_client(make_user(grants={"devices": ["view", "create", "edit"]}))
        assert editor.post(URL, FACE, format="json").status_code == 201
        assert editor.delete(detail(mapping)).status_code == 403


class TestCrud:
    def test_create_normalises_and_audits(self, admin_client):
        response = admin_client.post(URL, {**FACE, "device_platform": " ZMM220_TFT ", "notes": "  controlled test pending "}, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["meaning_code"] == "FACE" and body["device_platform"] == "ZMM220_TFT" and body["notes"] == "controlled test pending" and body["version"] == 1
        assert AuditLog.objects.filter(action="devices.protocol_mapping_created").count() == 1

    def test_live_scope_is_unique(self, admin_client):
        assert admin_client.post(URL, FACE, format="json").status_code == 201
        clash = admin_client.post(URL, FACE, format="json")
        assert clash.status_code == 409 and clash.json()["code"] == "protocol_mapping_exists"
        assert admin_client.post(URL, {**FACE, "firmware_version": "Ver 6.60"}, format="json").status_code == 201  # another scope
        first = ProtocolMapping.objects.get(firmware_version="")
        assert admin_client.delete(detail(first)).status_code == 204
        assert admin_client.post(URL, FACE, format="json").status_code == 201  # a deleted row does not block

    def test_validation(self, admin_client):
        bad = admin_client.post(URL, {"field": "verify", "raw_value": "x", "meaning_type": "COLOUR", "meaning_code": "9x"}, format="json")
        assert bad.status_code == 400 and {"field", "raw_value", "meaning_type", "meaning_code"} <= set(bad.json()["errors"])

    def test_patch_stale_and_unique(self, admin_client):
        mapping = ProtocolMappingFactory(raw_value=1, meaning_code="FINGERPRINT")
        ProtocolMappingFactory(raw_value=15)
        response = admin_client.patch(detail(mapping), {"confidence": "VERIFIED", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["confidence"] == "VERIFIED" and response.json()["version"] == 2
        assert admin_client.patch(detail(mapping), {"label": "x", "expected_version": 1}, format="json").json()["code"] == "stale_version"
        assert admin_client.patch(detail(mapping), {"raw_value": 15}, format="json").json()["code"] == "protocol_mapping_exists"
        unchanged = admin_client.patch(detail(mapping), {"confidence": "VERIFIED"}, format="json").json()
        assert unchanged["version"] == 2  # nothing changed, nothing written
        entry = AuditLog.objects.get(action="devices.protocol_mapping_updated")
        assert entry.before == {"confidence": "UNKNOWN"} and entry.after == {"confidence": "VERIFIED"}

    def test_filters(self, admin_client):
        ProtocolMappingFactory(field="status", raw_value=1)
        ProtocolMappingFactory(field="punch", raw_value=0, meaning_type="PUNCH_DIRECTION", meaning_code="IN")
        assert [row["meaning_code"] for row in admin_client.get(URL, {"field": "punch"}).json()["results"]] == ["IN"]


class TestObserved:
    def test_without_a_punch_store(self, admin_client):
        assert admin_client.get(f"{URL}observed/").json() == {"available": False, "status": [], "punch": []}

    def test_codes_with_counts_and_the_most_specific_mapping(self, admin_client, sink):
        device = DeviceFactory()
        records = [{"pin": "1", "device_time": datetime(2026, 9, 1, 9, minute), "status": status, "punch": 0} for minute, status in enumerate([15, 15, 1])]
        ingest.ingest(device, records, source=ingest.AGENT_PUSH)
        wildcard = ProtocolMappingFactory(field="status", raw_value=15, meaning_code="FACE")
        ProtocolMappingFactory(field="status", raw_value=15, meaning_code="PALM", device_platform="OTHER")
        body = admin_client.get(f"{URL}observed/").json()
        assert body["available"] is True
        assert [(row["raw_value"], row["count"], row["mapped_to"]) for row in body["status"]] == [(15, 2, "FACE"), (1, 1, None)]
        assert body["status"][0]["mapping_uid"] == str(wildcard.uid) and body["punch"][0]["raw_value"] == 0 and body["punch"][0]["count"] == 3
