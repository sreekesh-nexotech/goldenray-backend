"""``company/bank-accounts/`` — the accounts printed on quotations and agreements.

* the account number is Fernet-encrypted at rest and never returned in full (last four digits only);
* exactly one live account is primary (partial unique index): the first account becomes primary automatically,
  ``make-primary/`` moves the flag inside one transaction, and the primary account cannot be deleted while other
  accounts exist (make another one primary first);
* every write is audited (the account number is masked by the audit layer) and versioned.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from company.models import BankAccount
from core.errors import Conflict
from core.services import check_version, stamp_create

SNAPSHOT_FIELDS = ("label", "bank", "account_name", "account_number", "ifsc", "branch", "upi_id", "is_primary")
EDITABLE_FIELDS = ("label", "bank", "account_name", "account_number", "ifsc", "branch", "upi_id")


def accounts_queryset():
    return BankAccount.objects.order_by("-is_primary", "label", "id")


def primary_account() -> BankAccount | None:
    return BankAccount.objects.filter(is_primary=True).first()


def account_snapshot(account: BankAccount) -> dict:
    return snapshot(account, SNAPSHOT_FIELDS)


def _label_taken() -> Conflict:
    return Conflict("label_taken", "Another bank account already uses this label.", errors={"label": ["Already in use."]})


@transaction.atomic
def create_account(*, user, data) -> BankAccount:
    # Serialise primary changes: lock the current primary (if any) before deciding.
    current_primary = BankAccount.objects.select_for_update().filter(is_primary=True).first()
    wants_primary = bool(data.get("is_primary")) or current_primary is None
    if wants_primary and current_primary is not None:
        current_primary.versioned_update(user, is_primary=False)
        record("company.bank_account_updated", obj=current_primary, actor=user, before={"is_primary": True}, after={"is_primary": False})
    account = BankAccount(**{name: data.get(name, "") for name in EDITABLE_FIELDS}, is_primary=wants_primary)
    stamp_create(account, user)
    try:
        with transaction.atomic():
            account.save()
    except IntegrityError:
        raise _label_taken() from None
    record("company.bank_account_created", obj=account, actor=user, after=account_snapshot(account))
    return account


@transaction.atomic
def update_account(instance: BankAccount, *, user, data, expected_version=None) -> BankAccount:
    account = BankAccount.objects.select_for_update().get(pk=instance.pk)
    check_version(account, expected_version)
    before = account_snapshot(account)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data and data[name] != getattr(account, name)}
    if not values:
        return account
    try:
        with transaction.atomic():
            account.versioned_update(user, **values)
    except IntegrityError:
        raise _label_taken() from None
    changed_before, changed_after = changes(before, account_snapshot(account))
    record("company.bank_account_updated", obj=account, actor=user, before=changed_before, after=changed_after)
    return account


@transaction.atomic
def make_primary(instance: BankAccount, *, user, expected_version=None) -> BankAccount:
    current_primary = BankAccount.objects.select_for_update().filter(is_primary=True).first()
    account = BankAccount.objects.select_for_update().get(pk=instance.pk)
    check_version(account, expected_version)
    if account.is_primary:
        return account
    if current_primary is not None:
        current_primary.versioned_update(user, is_primary=False)
    account.versioned_update(user, is_primary=True)
    record(
        "company.bank_account_made_primary",
        obj=account,
        actor=user,
        before={"primary": str(current_primary.uid) if current_primary else None},
        after={"primary": str(account.uid)},
    )
    return account


@transaction.atomic
def delete_account(instance: BankAccount, *, user, expected_version=None) -> None:
    account = BankAccount.objects.select_for_update().get(pk=instance.pk)
    check_version(account, expected_version)
    if account.is_primary and BankAccount.objects.exclude(pk=account.pk).exists():
        raise Conflict("primary_account_protected", "Make another account primary before deleting the primary account.")
    account.soft_delete(user)
    record("company.bank_account_deleted", obj=account, actor=user, before=account_snapshot(account))
