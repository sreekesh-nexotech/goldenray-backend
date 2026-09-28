"""customers.services.legacy_import — Flarize customers.json (masked fixture), matched by phone only, idempotent."""

import copy
import json
from pathlib import Path

import pytest
from django.utils.dateparse import parse_datetime

from accounts.tests.factories import UserFactory
from audit.models import AuditLog
from core.models import LegacyMap
from customers.models import Customer
from customers.services import legacy_import
from customers.tests.factories import CustomerFactory

pytestmark = pytest.mark.django_db
ROWS = json.loads((Path(__file__).resolve().parent / "fixtures" / "flarize" / "customers.json").read_text())


def _target(customer_id):
    return Customer.all_objects.get(pk=LegacyMap.objects.get(source_system="FLARIZE", source_table="customers", source_id=customer_id).target_id)


def test_every_row_imports_with_its_code_and_timestamps():
    owner = UserFactory()
    LegacyMap.objects.create(source_system="FLARIZE", source_table="users", source_id="user-1788862142433-ba6751ae", target_table="accounts_user", target_id=owner.pk)
    result = legacy_import.import_flarize_customers(ROWS)
    assert result["created"] == 34 and result["updated"] == result["skipped"] == 0
    codes = sorted({violation["code"] for violation in result["violations"]})
    assert codes == ["unmapped_owner", "unparsable_phone"]
    assert sum(v["code"] == "unmapped_owner" for v in result["violations"]) == 6  # testuser, DemoSales, QA-Tester, unknown, two other ids
    first = ROWS[1]
    customer = _target(first["customerId"])
    assert (customer.code, customer.name, customer.pincode, customer.district, customer.state, str(customer.current_bill), customer.source) == (
        first["customerId"],
        first["name"],
        "695001",
        "Thiruvananthapuram",
        "Kerala",
        "3000.00",
        "SALES_ENTRY",
    )
    assert customer.phone_e164 == "+91" + first["phone"] and customer.created_at == parse_datetime(first["createdAt"])
    owned = [row for row in ROWS if row["createdBy"] == "user-1788862142433-ba6751ae"]
    assert Customer.objects.filter(owner=owner).count() == len(owned) == 28
    junk = _target(next(row["customerId"] for row in ROWS if row["phone"] == "12345678901"))
    assert (junk.phone_e164, junk.alt_phone) == ("", "12345678901")
    assert AuditLog.objects.get(action="customers.legacy_import").after["rows"] == 34


def test_rerun_is_idempotent_and_changes_update():
    legacy_import.import_flarize_customers(ROWS)
    again = legacy_import.import_flarize_customers(ROWS)
    assert (again["created"], again["updated"], again["skipped"]) == (0, 0, 34) and Customer.objects.count() == 34
    changed = copy.deepcopy(ROWS)
    changed[0].update(name="Renamed", billCycle="BIMONTHLY", updatedAt="2026-09-01T10:00:00.000Z")
    result = legacy_import.import_flarize_customers(changed)
    assert (result["created"], result["updated"]) == (0, 1)
    customer = _target(changed[0]["customerId"])
    assert (customer.name, customer.bill_cycle, customer.version) == ("Renamed", "BIMONTHLY", 2)


def test_matching_is_by_phone_only_never_by_name():
    same_phone = CustomerFactory(name="Somebody Else", phone_e164="+91" + ROWS[0]["phone"], email="", code="CUST-000001")
    CustomerFactory(name=ROWS[1]["name"], phone_e164="+919000000001")  # same name, other phone: not a match
    result = legacy_import.import_flarize_customers(ROWS[:2])
    assert result["created"] == 1 and result["updated"] == 1
    assert [v["code"] for v in result["violations"] if v["code"] == "matched_by_phone"] == ["matched_by_phone"]
    same_phone.refresh_from_db()
    assert (same_phone.name, same_phone.code, same_phone.address) == ("Somebody Else", "CUST-000001", ROWS[0]["address"])  # kept; gaps filled
    assert _target(ROWS[0]["customerId"]) == same_phone
    again = legacy_import.import_flarize_customers(ROWS[:2])
    assert again["created"] == 0 and Customer.objects.count() == 3
    same_phone.refresh_from_db()
    assert same_phone.name == "Somebody Else"


def test_row_violations():
    rows = [
        {
            "customerId": "CUST-X1",
            "name": "A",
            "phone": "9876543210",
            "email": "not-mail",
            "pincode": "12",
            "currentBill": "abc",
            "billCycle": "weekly",
            "source": "TV",
            "createdAt": "2026-08-27T15:44:41.370Z",
            "createdBy": None,
        },
        {"customerId": "", "name": "No id", "phone": "9876543211"},
        {"customerId": "CUST-" + "Y" * 30, "name": "Long id", "phone": "9876543212", "updatedBy": "ghost"},
    ]
    result = legacy_import.import_flarize_customers(rows)
    assert result["created"] == 2
    assert [v["code"] for v in result["violations"]] == ["invalid_email", "invalid_pincode", "invalid_bill", "unknown_bill_cycle", "unknown_source", "incomplete_row", "unmapped_user", "code_too_long"]
    first = Customer.objects.get(phone_e164="+919876543210")
    assert (first.email, first.pincode, first.current_bill, first.bill_cycle, first.source) == ("", "", None, "", "SALES_ENTRY")
    assert Customer.objects.get(phone_e164="+919876543212").code.startswith("CUST-0")


def test_dry_run_and_phone_matcher():
    result = legacy_import.import_flarize_customers(ROWS, dry_run=True)
    assert result["created"] == 34 and not Customer.objects.exists() and not LegacyMap.objects.exists()
    customer, created = legacy_import.match_or_create_by_phone(phone="98765 43210", name="Site owner")
    assert created and customer.source == "SI_IMPORT" and customer.code.startswith("CUST-")
    assert legacy_import.match_or_create_by_phone(phone="+919876543210", name="Other name") == (customer, False)
    assert legacy_import.match_or_create_by_phone(phone="123", name="x") == (None, False)


def test_phone_matcher_creates_customers_like_any_other_write():
    """The PA/SI importers' matcher is a write: the new customer is audited and the customers cache is bumped."""
    from flarize.cache_utils import get_versions

    before = get_versions(["customers"])["customers"]
    customer, created = legacy_import.match_or_create_by_phone(phone="98765 43210", name="Site owner", values={"district": "Kollam", "phone_e164": "+910000000000"})
    assert created and (customer.district, customer.phone_e164) == ("Kollam", "+919876543210")  # the matched phone wins
    audit = AuditLog.objects.get(action="customers.customer_created", object_uid=customer.uid)
    assert audit.after["phone_e164"] == "+919876543210" and audit.after["source"] == "SI_IMPORT"
    assert get_versions(["customers"])["customers"] > before
    legacy_import.match_or_create_by_phone(phone="+919876543210", name="Again")
    assert AuditLog.objects.filter(action="customers.customer_created").count() == 1  # a match writes nothing
