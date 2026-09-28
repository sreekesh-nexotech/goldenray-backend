"""Customer notes (``customers/<uid>/notes/``): add, edit or pin, delete (soft). Audited and versioned."""

from __future__ import annotations

from django.db import transaction

from audit.services import changes, record, snapshot
from core.services import check_version, stamp_create
from customers.models import Customer, CustomerNote
from customers.services.customers import CACHE_NAMESPACE
from flarize.cache_utils import bump

NOTE_FIELDS = ("body", "pinned")


def notes_queryset(customer: Customer):
    return CustomerNote.objects.filter(customer=customer).select_related("created_by").order_by("-pinned", "-created_at", "-id")


@transaction.atomic
def add_note(customer: Customer, *, user, body: str, pinned: bool = False) -> CustomerNote:
    note = CustomerNote(customer=customer, body=body, pinned=pinned)
    stamp_create(note, user)
    note.save()
    record("customers.note_added", obj=customer, actor=user, after={"note": str(note.uid), "pinned": pinned})
    bump(CACHE_NAMESPACE)
    return note


@transaction.atomic
def update_note(instance: CustomerNote, *, user, data: dict, expected_version=None) -> CustomerNote:
    note = CustomerNote.objects.select_for_update().get(pk=instance.pk)
    check_version(note, expected_version)
    values = {name: data[name] for name in NOTE_FIELDS if name in data and data[name] != getattr(note, name)}
    if not values:
        return note
    before = snapshot(note, NOTE_FIELDS)
    note.versioned_update(user, **values)
    changed_before, changed_after = changes(before, snapshot(note, NOTE_FIELDS))
    record("customers.note_updated", obj=note, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return note


@transaction.atomic
def delete_note(instance: CustomerNote, *, user, expected_version=None) -> None:
    note = CustomerNote.objects.select_for_update().get(pk=instance.pk)
    check_version(note, expected_version)
    note.soft_delete(user)
    record("customers.note_deleted", obj=note, actor=user, before=snapshot(note, NOTE_FIELDS))
    bump(CACHE_NAMESPACE)
