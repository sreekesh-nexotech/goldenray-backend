"""Database invariants of the devices tables (partial uniques among live rows, enum checks, ranges)."""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from devices.models import AdmsUnknownDevice, Agent, Device, DeviceUser, SyncLog
from devices.tests.factories import AgentFactory, DeviceFactory, DeviceUserFactory, ProtocolMappingFactory

pytestmark = pytest.mark.django_db


def refused(fn, name):
    with pytest.raises(IntegrityError) as caught, transaction.atomic():
        fn()
    assert name in str(caught.value)


@pytest.mark.parametrize(
    "values, name",
    [
        ({"name": ""}, "devices_device_name_not_blank"),
        ({"serial_number": "", "expected_serial": "NCDX"}, "devices_device_serial_not_blank"),
        ({"mac_address": "00-17-61-12-F2-D1"}, "devices_device_mac_format"),
        ({"expected_mac": "zz:17:61:12:f2:d1"}, "devices_device_expected_mac_format"),
        ({"port": 0}, "devices_device_port_range"),
        ({"protocol": "HTTP"}, "devices_device_protocol_valid"),
        ({"identity_status": "MAYBE"}, "devices_device_identity_status_valid"),
        ({"adms_enabled": True, "adms_token_hash": None}, "devices_device_adms_enabled_has_token"),
        ({"adms_token_hash": "not-hex"}, "devices_device_adms_token_hash_format"),
        ({"ip_address": None, "serial_number": None, "expected_serial": None}, "devices_device_locatable"),
    ],
)
def test_device_checks(values, name):
    refused(lambda: DeviceFactory(**values), name)


def test_live_uniques_ignore_deleted_rows():
    device = DeviceFactory(serial_number="NCD1", expected_serial="NCD1")
    refused(lambda: DeviceFactory(serial_number="NCD1", expected_serial="NCD2"), "devices_device_serial_live_uniq")
    Device.all_objects.filter(pk=device.pk).update(deleted_at=timezone.now())
    DeviceFactory(serial_number="NCD1", expected_serial="NCD1")
    row = DeviceUserFactory(pin="7")
    refused(lambda: DeviceUserFactory(device=row.device, pin="7"), "devices_device_user_pin_live_uniq")
    DeviceUserFactory(device=DeviceFactory(), pin="7")  # the same PIN on another terminal is another person
    refused(lambda: DeviceUser.objects.create(device=row.device, pin=""), "devices_device_user_pin_not_blank")


@pytest.mark.parametrize(
    "values, name",
    [
        ({"code": ""}, "devices_agent_code_not_blank"),
        ({"heartbeat_interval_seconds": 5}, "devices_agent_heartbeat_interval_range"),
        ({"sync_interval_seconds": 10}, "devices_agent_sync_interval_range"),
        ({"heartbeat_interval_seconds": 120, "offline_after_seconds": 60}, "devices_agent_offline_after_heartbeat"),
        ({"degraded_queue_threshold": 0}, "devices_agent_degraded_threshold_positive"),
    ],
)
def test_agent_checks(values, name):
    refused(lambda: AgentFactory(with_credential=False, **values), name)


def test_agent_code_is_unique_among_live_agents():
    agent = AgentFactory(code="A1", with_credential=False)
    refused(lambda: AgentFactory(code="A1", with_credential=False), "devices_agent_code_live_uniq")
    Agent.all_objects.filter(pk=agent.pk).update(deleted_at=timezone.now())
    AgentFactory(code="A1", with_credential=False)


def test_log_and_mapping_enums():
    device = DeviceFactory()
    refused(lambda: SyncLog.objects.create(device=device, sync_type="PHOTOS", status="SUCCESS"), "devices_sync_log_type_valid")
    refused(lambda: SyncLog.objects.create(device=device, sync_type="USERS", status="MAYBE"), "devices_sync_log_status_valid")
    refused(lambda: ProtocolMappingFactory(field="verify"), "devices_protocol_mapping_field_valid")
    refused(lambda: ProtocolMappingFactory(meaning_code=""), "devices_protocol_mapping_meaning_not_blank")
    ProtocolMappingFactory(raw_value=1)
    refused(lambda: ProtocolMappingFactory(raw_value=1), "devices_protocol_mapping_scope_live_uniq")
    refused(lambda: AdmsUnknownDevice.objects.create(serial_number=""), "devices_adms_unknown_serial_not_blank")
    refused(lambda: AdmsUnknownDevice.objects.create(serial_number="X", last_reason="WHO"), "devices_adms_unknown_reason_valid")


def test_string_forms():
    device = DeviceFactory(name="MARS-01", serial_number="NCD9", expected_serial="NCD9")
    assert "MARS-01" in str(device) and "NCD9" in str(device)
    assert str(ProtocolMappingFactory(raw_value=15, meaning_code="FACE")) == "status=15 → FACE"
