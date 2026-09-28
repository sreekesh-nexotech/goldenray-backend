"""Customer merge (``customers/<uid>/merge/``, ``customers.manage``) and the registry of rows that point at a customer.

Every model with a foreign key to ``Customer`` registers it once, from its app's ``AppConfig.ready()``::

    from customers.services import merge
    merge.register_dependant(Quotation, "customer", blocks_delete=True)   # later packages
    merge.register_dependant(Lead, "customer")                            # leads (this package)

Merging customer *A* into customer *B*, in one transaction:

1. both rows are locked (id order, so two concurrent merges cannot deadlock) and their ``expected_version`` checked;
2. every registered foreign key that points at *A* — live and soft-deleted rows alike, so history stays consistent —
   is re-pointed to *B* (``version`` bumped on versioned rows); notes move with the customer (they are registered);
3. empty fields of *B* are filled from *A* (never overwritten); *A*'s phone becomes *B*'s ``alt_phone`` when that is
   empty; *B* keeps its owner unless it has none;
4. *A* gets ``merged_into = B`` and is soft-deleted (freeing its phone number);
5. both rows get an audit entry (``customers.merged`` on *B* with the per-table counts, ``customers.merged_into`` on
   *A*) and ``customers.merged`` is emitted on the outbox (``{"source_uid", "target_uid", "repointed"}``) so contexts
   that keep customer data outside a registered foreign key can follow.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models, transaction
from django.db.models import F
from django.utils import timezone

from audit.services import changes, record
from core.errors import Conflict, DomainError, StaleVersion
from core.models import BaseModel, actor_or_none
from core.outbox import emit
from core.services import check_version
from customers.models import Customer, CustomerNote
from customers.services.customers import CACHE_NAMESPACE, customer_snapshot
from flarize.cache_utils import bump

FILLABLE_FIELDS = ("email", "address", "pincode", "district", "state", "location", "google_map_link", "latitude", "longitude", "current_bill", "bill_cycle", "lead")


@dataclass(frozen=True)
class Dependant:
    model: type[models.Model]
    field: str
    blocks_delete: bool

    @property
    def label(self) -> str:
        return f"{self.model._meta.app_label}.{self.model._meta.model_name}.{self.field}"

    def manager(self):
        return getattr(self.model, "all_objects", self.model._default_manager)

    def live_manager(self):
        return self.model._default_manager


_DEPENDANTS: dict[str, Dependant] = {}


def register_dependant(model: type[models.Model], field: str, *, blocks_delete: bool = False) -> None:
    """Register ``model.field`` (a ForeignKey to ``Customer``). Idempotent."""
    try:
        relation = model._meta.get_field(field)
    except FieldDoesNotExist as exc:
        raise ImproperlyConfigured(f"{model.__name__} has no field {field!r}.") from exc
    if not relation.many_to_one or relation.related_model is not Customer:
        raise ImproperlyConfigured(f"{model.__name__}.{field} is not a ForeignKey to customers.Customer.")
    dependant = Dependant(model, field, blocks_delete)
    _DEPENDANTS[dependant.label] = dependant


def unregister_dependant(model: type[models.Model], field: str) -> None:
    _DEPENDANTS.pop(Dependant(model, field, False).label, None)


def dependants() -> dict[str, Dependant]:
    return dict(_DEPENDANTS)


def references(customer: Customer) -> dict[str, int]:
    """``{label: live rows}`` for every registered foreign key that points at ``customer`` (zero counts omitted)."""
    found = {}
    for label, dependant in sorted(_DEPENDANTS.items()):
        count = dependant.live_manager().filter(**{dependant.field: customer}).count()
        if count:
            found[label] = count
    return found


def blocking_references(customer: Customer) -> dict[str, int]:
    return {label: count for label, count in references(customer).items() if _DEPENDANTS[label].blocks_delete}


def _repoint(dependant: Dependant, source: Customer, target: Customer, user) -> int:
    rows = dependant.manager().filter(**{dependant.field: source})
    values = {dependant.field: target}
    if issubclass(dependant.model, BaseModel):
        actor = actor_or_none(user)
        values.update(version=F("version") + 1, updated_at=timezone.now(), updated_by=actor)
    return rows.update(**values)


def _fill_blanks(source: Customer, target: Customer) -> dict:
    values = {}
    for name in FILLABLE_FIELDS:
        current = getattr(target, name)
        incoming = getattr(source, name)
        if current in (None, "") and incoming not in (None, ""):
            values[name] = incoming
    if not target.alt_phone and source.phone_e164 and source.phone_e164 != target.phone_e164:
        values["alt_phone"] = source.phone_e164
    if target.owner_id is None and source.owner_id is not None:
        values["owner"] = source.owner
    return values


@transaction.atomic
def merge_customers(source: Customer, *, into: Customer, user, expected_version=None, into_expected_version=None) -> Customer:
    """Merge ``source`` into ``into``; returns the survivor (see the module docstring)."""
    if source.pk == into.pk:
        raise DomainError("merge_into_self", "A customer cannot be merged into itself.", errors={"into_uid": ["Choose another customer."]})
    locked = {row.pk: row for row in Customer.all_objects.select_for_update().filter(pk__in=[source.pk, into.pk]).order_by("pk")}
    source, target = locked[source.pk], locked[into.pk]
    check_version(source, expected_version)
    if into_expected_version is not None and target.version != into_expected_version:
        raise StaleVersion(errors={"into_expected_version": [f"Current version is {target.version}."]})
    if source.deleted_at is not None or target.deleted_at is not None:
        raise Conflict("customer_already_merged", "One of the customers was already merged or deleted.")

    repointed = {}
    for label, dependant in sorted(_DEPENDANTS.items()):
        count = _repoint(dependant, source, target, user)
        if count:
            repointed[label] = count

    before = customer_snapshot(target)
    filled = _fill_blanks(source, target)
    source_phone = source.phone_e164
    source.versioned_update(user, merged_into=target, deleted_at=timezone.now())
    if filled:
        target.versioned_update(user, **filled)
    else:
        target.versioned_update(user)  # the survivor changed (it gained rows): bump its version
    changed_before, changed_after = changes(before, customer_snapshot(target))
    record(
        "customers.merged",
        obj=target,
        actor=user,
        before=changed_before,
        after={**changed_after, "merged_from": str(source.uid), "merged_from_phone": source_phone, "repointed": repointed},
    )
    record("customers.merged_into", obj=source, actor=user, after={"merged_into": str(target.uid)})
    emit(
        "customers.merged",
        {"source_uid": str(source.uid), "target_uid": str(target.uid), "repointed": repointed},
        aggregate_type="customers.customer",
        aggregate_uid=target.uid,
        dedup_key=f"customers.merged:{source.uid}",
    )
    bump(CACHE_NAMESPACE)
    return target


def register_builtin_dependants() -> None:
    """The customers app's own references (called from ``CustomersConfig.ready``)."""
    register_dependant(CustomerNote, "customer")
    register_dependant(Customer, "merged_into")
