"""Customer records (PLAN §2.6, §3.4 Sales ``customers/``).

* one live customer per phone number (partial unique index); a clash is 409 ``phone_taken`` — the existing
  customer's uid is only disclosed to users who may see it;
* ``owner`` is the record-scope anchor: Sales Executives (``owned``) see and edit their own customers. A new customer
  is owned by its creator; giving it (or re-assigning it) to someone else needs ``customers.manage``;
* soft delete only, refused (409 ``customer_in_use``) while a live dependant that blocks deletion (quotations,
  agreements … registered in :mod:`customers.services.merge`) references the customer;
* every write is versioned, audited and bumps the ``customers`` cache namespace.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from accounts.services.authz import can
from audit.services import changes, record, snapshot
from core import scopes
from core.errors import Conflict, PermissionDenied
from core.sequences import next_number
from core.services import check_version, stamp_create
from customers.models import Customer, CustomerNote
from flarize.cache_utils import bump

CACHE_NAMESPACE = "customers"
MODULE = "customers"
EDITABLE_FIELDS = (
    "name",
    "phone_e164",
    "alt_phone",
    "email",
    "address",
    "pincode",
    "district",
    "state",
    "location",
    "google_map_link",
    "latitude",
    "longitude",
    "current_bill",
    "bill_cycle",
    "source",
    "owner",
)
SNAPSHOT_FIELDS = ("code", *EDITABLE_FIELDS, "lead", "merged_into")


# ``customers`` owned scope: how each model of the module reaches its customer's owner. Unknown models see nothing.
OWNER_PATHS: dict[type, str] = {}


def register_owner_path(model: type, path: str) -> None:
    OWNER_PATHS[model] = path


@scopes.register(MODULE, "owned")
def owned_customers(queryset, user):
    path = OWNER_PATHS.get(queryset.model)
    return queryset.filter(**{path: user}) if path else queryset.none()


register_owner_path(Customer, "owner")
register_owner_path(CustomerNote, "customer__owner")


def customers_queryset():
    return Customer.objects.select_related("owner", "lead")


def visible_customers(user):
    return scopes.apply(customers_queryset(), user, MODULE)


def customer_snapshot(customer: Customer) -> dict:
    return snapshot(customer, SNAPSHOT_FIELDS)


def next_code() -> str:
    """``CUST-000001`` … (taken in the caller's transaction; migrated Flarize codes keep their own shape)."""
    return next_number("CUST", fmt=lambda n, period: f"CUST-{n:06d}")


def find_by_phone(phone_e164: str, *, lock: bool = False) -> Customer | None:
    if not phone_e164:
        return None
    queryset = Customer.objects.filter(phone_e164=phone_e164)
    if lock:
        queryset = queryset.select_for_update()
    return queryset.first()


def phone_taken(user, phone_e164: str, existing: Customer | None = None) -> Conflict:
    existing = existing or find_by_phone(phone_e164)
    errors = {"phone": ["A customer with this phone number already exists."]}
    if existing is not None and visible_customers(user).filter(pk=existing.pk).exists():
        errors["existing_customer"] = [str(existing.uid)]
    return Conflict("phone_taken", "A customer with this phone number already exists.", errors=errors)


_NEW = object()


def _check_owner(user, owner, current=_NEW) -> None:
    """Creating a customer for someone else, or changing an owner, needs ``customers.manage``."""
    if current is _NEW:
        if owner is not None and owner.pk == getattr(user, "pk", None):
            return
    elif owner == current:
        return
    if not can(user, MODULE, "manage"):
        raise PermissionDenied("owner_change_forbidden", "Choosing another owner for a customer needs the customers manage permission.", errors={"owner_uid": ["Not allowed."]})


def _save_new(customer: Customer, user) -> None:
    try:
        with transaction.atomic():
            customer.save()
    except IntegrityError:
        raise phone_taken(user, customer.phone_e164) from None


@transaction.atomic
def create_customer(*, user, data: dict) -> Customer:
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data and data[name] is not None}
    owner = data.get("owner", user if getattr(user, "is_authenticated", False) else None)
    _check_owner(user, owner)
    values["owner"] = owner
    phone = values.get("phone_e164", "")
    existing = find_by_phone(phone)
    if existing is not None:
        raise phone_taken(user, phone, existing)
    customer = Customer(code=next_code(), lead=data.get("lead"), **values)
    stamp_create(customer, user)
    _save_new(customer, user)
    record("customers.customer_created", obj=customer, actor=user, after=customer_snapshot(customer))
    bump(CACHE_NAMESPACE)
    return customer


@transaction.atomic
def update_customer(instance: Customer, *, user, data: dict, expected_version=None) -> Customer:
    customer = Customer.objects.select_for_update().get(pk=instance.pk)
    check_version(customer, expected_version)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data and data[name] != getattr(customer, name)}
    if "owner" in values:
        _check_owner(user, values["owner"], customer.owner)
    if values.get("phone_e164"):
        existing = Customer.objects.filter(phone_e164=values["phone_e164"]).exclude(pk=customer.pk).first()
        if existing is not None:
            raise phone_taken(user, values["phone_e164"], existing)
    if not values:
        return customer
    before = customer_snapshot(customer)
    try:
        with transaction.atomic():
            customer.versioned_update(user, **values)
    except IntegrityError:
        raise phone_taken(user, values.get("phone_e164", customer.phone_e164)) from None
    changed_before, changed_after = changes(before, customer_snapshot(customer))
    record("customers.customer_updated", obj=customer, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return customer


@transaction.atomic
def delete_customer(instance: Customer, *, user, expected_version=None) -> None:
    from customers.services.merge import blocking_references

    customer = Customer.objects.select_for_update().get(pk=instance.pk)
    check_version(customer, expected_version)
    blocking = blocking_references(customer)
    if blocking:
        raise Conflict(
            "customer_in_use", "This customer is referenced by live records; merge it instead of deleting it.", errors={label: [f"{count} live row(s)."] for label, count in blocking.items()}
        )
    customer.soft_delete(user)
    record("customers.customer_deleted", obj=customer, actor=user, before=customer_snapshot(customer))
    bump(CACHE_NAMESPACE)


@transaction.atomic
def ensure_customer(*, user, values: dict, lead=None, source: str = Customer.Source.WEBSITE, owner=None) -> tuple[Customer, bool]:
    """The live customer with ``values["phone_e164"]`` (matched by phone only, never by name), else a new one.

    Used by lead conversion. Returns ``(customer, created)``. A matched customer keeps its data and owner.
    """
    phone = values.get("phone_e164", "")
    existing = find_by_phone(phone, lock=True)
    if existing is not None:
        return existing, False
    fields = {name: value for name, value in values.items() if name in EDITABLE_FIELDS and value not in (None, "")}
    fields.setdefault("source", source)
    customer = Customer(code=next_code(), lead=lead, owner=owner, **fields)
    stamp_create(customer, user)
    _save_new(customer, user)
    record("customers.customer_created", obj=customer, actor=user, after=customer_snapshot(customer), note="converted from a lead" if lead is not None else "")
    bump(CACHE_NAMESPACE)
    return customer, True
