"""``procurement/suppliers/`` — supplier master (PLAN §2.4).

Codes are unique case-insensitively among live suppliers; a supplier with batches cannot be deleted (409
``supplier_in_use`` — deactivate it instead). ``contact`` is free metadata: an object of short strings.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError
from core.services import check_version, stamp_create
from procurement.models import Batch, Supplier

FIELDS = ("code", "name", "gstin", "contact", "address", "reference", "is_active")


def suppliers_queryset():
    return Supplier.objects.order_by("code", "id")


def _clean(values: dict) -> dict:
    if "code" in values:
        values["code"] = (values["code"] or "").strip()
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
        if not values["name"]:
            raise DomainError("validation_error", "A supplier needs a name.", errors={"name": ["This field may not be blank."]})
    if "gstin" in values:
        values["gstin"] = (values["gstin"] or "").strip().upper()
    contact = values.get("contact")
    if contact is not None:
        if not isinstance(contact, dict) or any(not isinstance(key, str) or not isinstance(value, str) or len(value) > 200 for key, value in contact.items()) or len(contact) > 20:
            raise DomainError("validation_error", "contact is an object of short strings.", errors={"contact": ['{"name": "…", "phone": "…", "email": "…"} — strings ≤ 200 characters.']})
    return values


def _conflict() -> Conflict:
    return Conflict("supplier_code_taken", "Another supplier already uses this code.", errors={"code": ["Already in use."]})


@transaction.atomic
def create_supplier(*, user, data: dict) -> Supplier:
    values = _clean({name: data[name] for name in FIELDS if name in data})
    supplier = Supplier(**values)
    stamp_create(supplier, user)
    try:
        with transaction.atomic():
            supplier.save()
    except IntegrityError:
        raise _conflict() from None
    record("procurement.supplier_created", obj=supplier, actor=user, after=snapshot(supplier, FIELDS))
    return supplier


@transaction.atomic
def update_supplier(instance: Supplier, *, user, data: dict, expected_version=None) -> Supplier:
    supplier = Supplier.objects.select_for_update().get(pk=instance.pk)
    check_version(supplier, expected_version)
    values = _clean({name: data[name] for name in FIELDS if name in data})
    values = {name: value for name, value in values.items() if getattr(supplier, name) != value}
    if not values:
        return supplier
    before = snapshot(supplier, FIELDS)
    try:
        with transaction.atomic():
            supplier.versioned_update(user, **values)
    except IntegrityError:
        raise _conflict() from None
    changed_before, changed_after = changes(before, snapshot(supplier, FIELDS))
    record("procurement.supplier_updated", obj=supplier, actor=user, before=changed_before, after=changed_after)
    return supplier


@transaction.atomic
def delete_supplier(instance: Supplier, *, user, expected_version=None) -> None:
    supplier = Supplier.objects.select_for_update().get(pk=instance.pk)
    check_version(supplier, expected_version)
    batches = Batch.objects.filter(supplier=supplier).count()
    if batches:
        raise Conflict("supplier_in_use", f"{batches} batches name this supplier; deactivate it instead.", errors={"batches": [str(batches)]})
    supplier.soft_delete(user)
    record("procurement.supplier_deleted", obj=supplier, actor=user, before=snapshot(supplier, FIELDS))
