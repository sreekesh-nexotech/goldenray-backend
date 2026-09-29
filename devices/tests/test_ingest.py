"""Shared punch ingestion and the punch-sink interface (A8, A10, A11)."""

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from devices.models import SyncLog
from devices.services import ingest, punch_sink
from devices.tests.conftest import events
from devices.tests.factories import DeviceFactory

pytestmark = pytest.mark.django_db


class TestDedupKey:
    def test_same_content_same_key_whatever_the_transport(self):
        at = datetime(2026, 9, 22, 9, 31, 5)
        assert punch_sink.dedup_key("NCD1", "7", at, 15, 0) == punch_sink.dedup_key("NCD1", "7", at.replace(microsecond=999), 15, 0)
        assert punch_sink.dedup_key("NCD1", "7", at, 15, 0) != punch_sink.dedup_key("NCD1", "7", at, 1, 0)
        assert punch_sink.dedup_key("NCD1", "7", at, None, None) != punch_sink.dedup_key("NCD1", "7", at, 0, 0)
        assert len(punch_sink.dedup_key("", "7", at, None, None)) == 64


class TestParsing:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("2026-09-22 09:31:05", datetime(2026, 9, 22, 9, 31, 5)),
            ("2026-09-22T09:31:05", datetime(2026, 9, 22, 9, 31, 5)),
            ("2026/09/22 09:31:05", datetime(2026, 9, 22, 9, 31, 5)),
            ("2026-09-22 09:31", datetime(2026, 9, 22, 9, 31)),
            ("2026-09-22T09:31:05+05:30", datetime(2026, 9, 22, 9, 31, 5)),  # the offset is dropped, never converted
            ("2026-09-22T09:31:05Z", datetime(2026, 9, 22, 9, 31, 5)),
            ("yesterday", None),
            ("", None),
            (None, None),
            (12, None),
        ],
    )
    def test_device_time_is_the_wall_clock(self, value, expected):
        assert ingest.parse_device_time(value) == expected

    def test_invalid_records_are_counted(self):
        device = DeviceFactory()
        future = (timezone.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        records = [
            {"pin": "", "device_time": "2026-09-01 09:00:00"},
            {"pin": "x" * 81, "device_time": "2026-09-01 09:00:00"},
            {"pin": "1", "device_time": "1999-12-31 23:59:59"},
            {"pin": "1", "device_time": future},
            {"pin": "1", "device_time": "2026-09-01 09:00:00", "status": "15", "punch": "x"},
            {"pin": "1", "device_time": "2026-09-01 09:00:00", "status": 15},
        ]
        punches, invalid, duplicates = ingest.build_punches(device, records, source=ingest.AGENT_PUSH)
        assert invalid == 4 and duplicates == 1 and len(punches) == 1
        assert (punches[0].status_code, punches[0].punch_code, punches[0].device_serial, punches[0].office_timezone) == (15, None, device.serial_number, "Asia/Kolkata")


class TestIngest:
    def test_null_sink_keeps_nothing_and_says_so(self):
        device = DeviceFactory()
        counts = ingest.ingest(device, [{"pin": "1", "device_time": "2026-09-01 09:00:00"}], source=ingest.AGENT_PUSH, batch_id="b1")
        assert counts == {"received": 1, "new": 0, "duplicate": 0, "invalid": 0, "discarded": 1}
        log = SyncLog.objects.get(device=device)
        assert log.details["punch_store_installed"] is False and log.details["discarded"] == 1 and log.details["batch_id"] == "b1" and events(ingest.EVENT) == []
        device.refresh_from_db()
        assert device.attendance_count == 0 and device.last_punch_at is not None

    def test_new_punches_are_announced_with_their_local_dates(self, sink):
        device = DeviceFactory()
        records = [{"pin": "1", "device_time": "2026-09-01 23:59:00"}, {"pin": "2", "device_time": "2026-09-02 00:01:00"}, {"pin": "", "device_time": "x"}]
        counts = ingest.ingest(device, records, source=ingest.ADMS_PUSH)
        assert counts == {"received": 3, "new": 2, "duplicate": 0, "invalid": 1, "discarded": 0}
        [event] = events(ingest.EVENT)
        assert event["dates"] == ["2026-09-01", "2026-09-02"] and event["date_from"] == "2026-09-01" and event["date_to"] == "2026-09-02" and event["unmapped_pins"] == ["1", "2"]
        log = SyncLog.objects.get(device=device)
        assert log.status == SyncLog.Status.PARTIAL and "1 record(s)" in log.error_message and log.records_new == 2
        device.refresh_from_db()
        assert device.attendance_count == 2  # last_punch_at: the wall clock read in the office zone
        assert timezone.localtime(device.last_punch_at, ingest.office_zone(device)).strftime("%Y-%m-%d %H:%M") == "2026-09-02 00:01"

    def test_an_empty_batch_writes_no_log(self):
        device = DeviceFactory()
        assert ingest.ingest(device, [], source=ingest.AGENT_PUSH)["received"] == 0 and not SyncLog.objects.filter(device=device).exists()

    def test_registering_a_sink(self):
        class Kept(punch_sink.PunchSink):
            installed = True

        sink = punch_sink.register(Kept())
        assert punch_sink.get() is sink and punch_sink.installed()
        punch_sink.reset()
        assert not punch_sink.installed() and isinstance(punch_sink.get(), punch_sink.NullSink)
        assert punch_sink.get().status(None)["stored_records"] is None and punch_sink.get().pin_activity(None) == [] and punch_sink.get().observed_codes() == []
