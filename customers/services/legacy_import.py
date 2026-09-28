"""Import of Flarize ``data/customers.json`` (PLAN §7.4) — called by ``migrations_tools`` (``import_flarize``).

Contract: :mod:`customers.services.import_support` (plain row dicts in, ``{"created", "updated", "skipped",
"violations"}`` out, idempotent through ``core_legacy_map`` as ``FLARIZE`` / ``customers`` / ``customerId``).

Rules:

* **matching is by phone only, never by name**: an unmapped row whose E.164 phone belongs to a live customer is
  linked to that customer (its empty fields are filled, nothing is overwritten, the existing ``code`` stays) and
  listed as ``matched_by_phone``; otherwise a customer is created with ``code = customerId``;
* ``phone`` → ``phone_e164`` (region IN). A number that does not parse is imported as an empty phone and the raw value
  is kept in ``alt_phone`` (``unparsable_phone``) so staff can fix it — the customer is not dropped, because Flarize
  quotations refer to it;
* ``createdBy`` → ``owner`` and ``created_by``, ``updatedBy`` → ``updated_by`` through the imported Flarize users
  (``FLARIZE`` / ``users``); an id without an imported user leaves the owner empty (``unmapped_owner``);
* ``email`` is lower-cased; one that is not an address is dropped (``invalid_email``); ``pincode`` must be six
  digits (``invalid_pincode``, dropped); ``currentBill`` → ``current_bill``; ``billCycle`` → ``bill_cycle``
  (MONTHLY/BIMONTHLY, anything else dropped with ``unknown_bill_cycle``); ``source`` must be a known source
  (``unknown_source`` → SALES_ENTRY); ``createdAt``/``updatedAt`` are preserved;
* a row without ``customerId`` or ``name`` is skipped (``incomplete_row``).

:func:`match_or_create_by_phone` is the same phone-only rule for the later Purchase-Agreement and Site-Inspection
importers (PLAN §7.5: "customers by phone → customers_customer (unmatched created, source=SI_IMPORT)").
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from core.models import LegacyMap
from core.services import stamp_create
from customers.models import Customer
from customers.services.customers import CACHE_NAMESPACE, find_by_phone, next_code
from customers.services.import_support import ImportRun, mapped_id, run_import, timestamp, upsert
from customers.services.phones import try_normalise

FLARIZE = LegacyMap.SourceSystem.FLARIZE
SOURCE_TABLE = "customers"
USERS_TABLE = "users"
ACTION = "customers.legacy_import"
PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")
BILL_CYCLES = {"MONTHLY": Customer.BillCycle.MONTHLY, "BIMONTHLY": Customer.BillCycle.BIMONTHLY, "BI_MONTHLY": Customer.BillCycle.BIMONTHLY}


def _text(value, limit: int) -> str:
    return str(value).strip()[:limit] if value not in (None, "") else ""


def _user_id(run: ImportRun, row_id: str, legacy_user, column: str) -> int | None:
    if legacy_user in (None, ""):
        return None
    target = mapped_id(FLARIZE, USERS_TABLE, legacy_user)
    if target is None:
        run.violation(row_id, "unmapped_owner" if column == "createdBy" else "unmapped_user", f"{column}={legacy_user!r} has no imported user; left empty.")
    return target


def _values(run: ImportRun, row: dict, row_id: str) -> dict:
    values = {"name": _text(row.get("name"), 255)}
    raw_phone = _text(row.get("phone"), 32)
    phone = try_normalise(raw_phone) if raw_phone else None
    values["phone_e164"] = phone or ""
    if raw_phone and phone is None:
        values["alt_phone"] = raw_phone[:20]
        run.violation(row_id, "unparsable_phone", f"phone={raw_phone!r} is not a valid number; imported without a phone (raw value kept in alt_phone).")
    email = _text(row.get("email"), 254).lower()
    if email:
        try:
            validate_email(email)
            values["email"] = email
        except ValidationError:
            run.violation(row_id, "invalid_email", f"email={email!r} is not an address; left empty.")
    pincode = _text(row.get("pincode"), 16)
    if pincode:
        if PINCODE_RE.match(pincode):
            values["pincode"] = pincode
        else:
            run.violation(row_id, "invalid_pincode", f"pincode={pincode!r} is not six digits; left empty.")
    for source_key, target in (("address", "address"), ("district", "district"), ("state", "state")):
        values[target] = _text(row.get(source_key), 100 if target != "address" else 10_000)
    bill = row.get("currentBill")
    if bill not in (None, ""):
        try:
            amount = Decimal(str(bill)).quantize(Decimal("0.01"))
            if amount < 0 or amount >= Decimal("100000000"):
                raise InvalidOperation
            values["current_bill"] = amount
        except InvalidOperation:
            run.violation(row_id, "invalid_bill", f"currentBill={bill!r} is not an amount; left empty.")
    cycle = _text(row.get("billCycle"), 32).upper()
    if cycle:
        if cycle in BILL_CYCLES:
            values["bill_cycle"] = BILL_CYCLES[cycle]
        else:
            run.violation(row_id, "unknown_bill_cycle", f"billCycle={cycle!r}; left empty.")
    source = _text(row.get("source"), 32).upper() or Customer.Source.SALES_ENTRY
    if source not in Customer.Source.values:
        run.violation(row_id, "unknown_source", f"source={source!r} imported as SALES_ENTRY.")
        source = Customer.Source.SALES_ENTRY
    values["source"] = source
    return values


def _fill_only(target: Customer, values: dict) -> dict:
    """For a customer matched by phone: keep what it has, take only what it lacks."""
    return {name: value for name, value in values.items() if getattr(target, name) in (None, "") and value not in (None, "")}


def _import_customer(run: ImportRun, row: dict) -> None:
    row_id = _text(row.get("customerId"), 128)
    if not row_id or not _text(row.get("name"), 255):
        run.violation(row_id or "?", "incomplete_row", "customerId and name are required; row skipped.")
        return
    values = _values(run, row, row_id)
    owner_id = _user_id(run, row_id, row.get("createdBy"), "createdBy")
    updated_by_id = _user_id(run, row_id, row.get("updatedBy"), "updatedBy")
    target = run.find_target(Customer, row_id)
    if target is None and values["phone_e164"]:
        target = find_by_phone(values["phone_e164"])
        if target is not None:
            run.violation(row_id, "matched_by_phone", f"linked to existing customer {target.code} with the same phone; only its empty fields were filled.")
    if target is not None and target.code != row_id:
        # A customer this row was matched to by phone (or re-coded): it keeps its data; only gaps are filled.
        upsert(run, Customer, row_id, target=target, values=_fill_only(target, {**values, "owner_id": owner_id}))
        return
    if target is None:
        if len(row_id) > 24:
            run.violation(row_id, "code_too_long", "customerId is longer than 24 characters; a new CUST- code was issued.")
        code = row_id if len(row_id) <= 24 and not Customer.all_objects.filter(code=row_id).exists() else next_code()
        values["code"] = code
    values["owner_id"] = owner_id
    created_at = timestamp(row.get("createdAt"))
    upsert(run, Customer, row_id, target=target, values=values, created_at=created_at, updated_at=timestamp(row.get("updatedAt")) or created_at, created_by_id=owner_id, updated_by_id=updated_by_id)


def import_flarize_customers(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    """Import Flarize ``customers.json`` rows (file order)."""
    return run_import(ImportRun(FLARIZE, SOURCE_TABLE), rows, _import_customer, user=user, dry_run=dry_run, action=ACTION, object_type="customers.customer", namespaces=(CACHE_NAMESPACE,))


@transaction.atomic
def match_or_create_by_phone(*, phone, name: str, user=None, source: str = Customer.Source.SI_IMPORT, values: dict | None = None) -> tuple[Customer | None, bool]:
    """The live customer with this phone (``(customer, False)``), else a new one (``(customer, True)``).

    Never matches by name; an unparsable phone returns ``(None, False)`` so the caller reports it.
    """
    phone_e164 = try_normalise(phone)
    if phone_e164 is None:
        return None, False
    existing = find_by_phone(phone_e164, lock=True)
    if existing is not None:
        return existing, False
    customer = Customer(code=next_code(), name=(name or "").strip()[:255] or phone_e164, phone_e164=phone_e164, source=source, **(values or {}))
    stamp_create(customer, user)
    customer.save()
    return customer, True
