import threading
from datetime import date

import pytest
from django.db import connection, transaction

from core.models import SequenceCounter
from core.sequences import day_key, ensure_next_value_at_least, fiscal_year_key, next_number, next_value, resolve_period


@pytest.mark.parametrize(("day", "expected"), [(date(2026, 3, 31), "2025-26"), (date(2026, 4, 1), "2026-27"), (date(2026, 9, 28), "2026-27"), (date(2099, 12, 31), "2099-00")])
def test_fiscal_year_key(day, expected):
    assert fiscal_year_key(day) == expected


def test_day_key_and_period_resolution():
    assert day_key(date(2026, 9, 28)) == "20260928"
    assert resolve_period("SV", on=date(2026, 9, 28)) == "20260928"
    assert resolve_period("AGR", on=date(2026, 9, 28)) == "2026-27"
    assert resolve_period("QUO") == ""
    assert resolve_period("SV", "20260101") == "20260101"


@pytest.mark.django_db
class TestFormats:
    def test_quotation_numbers(self):
        assert [next_number("QUO") for _ in range(3)] == ["GR-1", "GR-2", "GR-3"]

    def test_agreement_numbers_restart_per_financial_year(self):
        assert next_number("AGR", on=date(2026, 9, 1)) == "AGR-2026-27-0001"
        assert next_number("AGR", on=date(2027, 3, 31)) == "AGR-2026-27-0002"
        assert next_number("AGR", on=date(2027, 4, 1)) == "AGR-2027-28-0001"

    def test_site_visit_numbers_restart_per_day(self):
        assert next_number("SV", on=date(2026, 9, 28)) == "SV-20260928-0001"
        assert next_number("SV", on=date(2026, 9, 28)) == "SV-20260928-0002"
        assert next_number("SV", on=date(2026, 9, 29)) == "SV-20260929-0001"

    def test_lead_and_project_numbers(self):
        assert next_number("LEAD") == "L-1"
        assert next_number("PROJ") == "PROJ-1"
        assert next_number("LEAD") == "L-2"

    def test_custom_formats(self):
        assert next_number("BATCH", "2026", fmt="BATCH-{period}-{n:03d}") == "BATCH-2026-001"
        assert next_number("CUST", fmt=lambda n, period: f"CUST-{n:05d}") == "CUST-00001"

    def test_unknown_kind_needs_a_format(self):
        with pytest.raises(ValueError):
            next_number("NOPE")

    @pytest.mark.parametrize(("kind", "period"), [("lower", ""), ("", ""), ("A" * 17, ""), ("QUO", "bad period!")])
    def test_invalid_keys(self, kind, period):
        with pytest.raises(ValueError):
            next_value(kind, period)

    def test_importer_can_continue_a_legacy_counter_but_never_rewind(self):
        assert ensure_next_value_at_least("QUO", 9730) == 9730
        assert next_number("QUO") == "GR-9730"
        assert ensure_next_value_at_least("QUO", 100) == 9731
        assert next_number("QUO") == "GR-9731"
        with pytest.raises(ValueError):
            ensure_next_value_at_least("QUO", 0)

    def test_counter_row_is_created_on_first_use(self):
        assert not SequenceCounter.objects.filter(kind="LEAD").exists()
        next_value("LEAD")
        assert SequenceCounter.objects.get(kind="LEAD", period_key="").next_value == 2


@pytest.mark.django_db(transaction=True)
def test_numbers_require_the_callers_transaction():
    with pytest.raises(RuntimeError):
        next_number("QUO")


@pytest.mark.django_db(transaction=True)
def test_rolled_back_documents_leave_no_gap():
    with transaction.atomic():
        assert next_number("QUO") == "GR-1"
    try:
        with transaction.atomic():
            assert next_number("QUO") == "GR-2"
            raise RuntimeError("document insert failed")
    except RuntimeError:
        pass
    with transaction.atomic():
        assert next_number("QUO") == "GR-2"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["LEAD", "SV"])
def test_concurrent_numbers_are_unique_and_gapless(kind):
    threads_count, per_thread = 8, 6
    results: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads_count)

    def worker():
        try:
            barrier.wait()
            for _ in range(per_thread):
                with transaction.atomic():
                    number = next_number(kind, on=date(2026, 9, 28))
                with lock:
                    results.append(number)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    values = sorted(int(number.rsplit("-", 1)[1]) for number in results)
    assert values == list(range(1, threads_count * per_thread + 1))
