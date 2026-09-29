"""The partitioned evidence table: monthly partitions, the 30-day purge, the Beat task and the command."""

from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from devices.models import AdmsRequest
from devices.services import adms, adms_evidence
from devices.tasks import purge_adms_evidence

pytestmark = pytest.mark.django_db


def evidence(received_at) -> AdmsRequest:
    return AdmsRequest.objects.create(id=adms._next_id(), received_at=received_at, method="POST", path="/iclock/[redacted]/cdata")


def rows_in(table: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {connection.ops.quote_name(table)}")
        return cursor.fetchone()[0]


def test_the_migration_created_this_month_and_the_next_two():
    this = timezone.now().date().replace(day=1)
    expected = {adms_evidence.Month(adms_evidence.add_months(this, offset)).name for offset in range(3)}
    assert expected <= adms_evidence.existing_partitions() and adms_evidence.missing_upcoming(2) == []
    assert adms_evidence.connected_as_owner() is True


def test_month_arithmetic_and_names():
    assert adms_evidence.add_months(date(2026, 11, 1), 3) == date(2027, 2, 1)
    month = adms_evidence.Month(date(2026, 12, 1))
    assert month.name == "devices_adms_request_y2026m12" and month.upper == "'2027-01-01 00:00:00+00'"
    assert adms_evidence.month_of("devices_adms_request_y2026m12") == month and adms_evidence.month_of("devices_adms_request_default") is None
    with pytest.raises(ValueError):
        adms_evidence.ensure_partitions(0)


def test_rows_stranded_in_the_default_partition_move_into_their_month():
    later = datetime(2031, 3, 15, 12, tzinfo=dt_timezone.utc)
    evidence(later)
    assert rows_in(adms_evidence.DEFAULT_PARTITION) == 1
    created = adms_evidence.ensure_partitions(1, today=date(2031, 3, 20))
    assert created == ["devices_adms_request_y2031m03"] and rows_in("devices_adms_request_y2031m03") == 1 and rows_in(adms_evidence.DEFAULT_PARTITION) == 0
    assert adms_evidence.ensure_partitions(1, today=date(2031, 3, 20)) == []  # idempotent
    assert AdmsRequest.objects.filter(received_at=later).count() == 1


def test_purge_drops_old_partitions_and_deletes_old_rows():
    now = timezone.now()
    old_month = adms_evidence.add_months(now.date().replace(day=1), -3)
    adms_evidence.ensure_partitions(1, today=old_month)
    evidence(datetime.combine(old_month, datetime.min.time(), tzinfo=dt_timezone.utc) + timedelta(days=3))
    evidence(now - timedelta(days=31))
    kept = evidence(now - timedelta(days=29))
    result = adms_evidence.purge()
    assert result["dropped"] == [adms_evidence.Month(old_month).name] and result["deleted"] == 1
    assert list(AdmsRequest.objects.values_list("id", flat=True)) == [kept.id]
    assert adms_evidence.Month(old_month).name not in adms_evidence.existing_partitions()


def test_the_beat_task_and_the_command():
    evidence(timezone.now() - timedelta(days=40))
    result = purge_adms_evidence()
    assert result["owner"] is True and result["deleted"] == 1
    out = StringIO()
    call_command("maintain_adms_evidence", "--months", "4", stdout=out)
    assert "partitions created: devices_adms_request_y" in out.getvalue() and "purged before" in out.getvalue()
    out = StringIO()
    call_command("maintain_adms_evidence", "--no-purge", stdout=out)
    assert out.getvalue().strip() == "partitions created: none"


def test_the_body_cap_is_a_database_constraint():
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        AdmsRequest.objects.create(id=adms._next_id(), method="POST", path="/x", body=b"x" * (1024 * 1024 + 1))
