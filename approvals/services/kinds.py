"""The closed set of changes that can go through a change request, registered by the apps that own them.

The platform never imports a product app: each owner (bom, catalog, pricing) registers its kinds from
``AppConfig.ready``. A kind knows how to validate a submitted body, apply it as a user, snapshot the target and name
it, and which ``(module, action)`` applying it needs — the approver must hold that grant, so approving can never do
more than the approver could do directly.

:func:`viewset_kind` builds a kind from a CRUD viewset: every staff viewset writes through its ``services`` map
(``core.views.mixins``) and validates with its ``get_serializer_class()``, so applying a request runs exactly what
the direct ``POST``/``PATCH``/``DELETE`` would have run — same validation, audit, cache bumps and outbox events.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from django.core.serializers.json import DjangoJSONEncoder

from core.errors import DomainError

CREATE, UPDATE, DELETE, SET = "CREATE", "UPDATE", "DELETE", "SET"
#: The DRF action a viewset runs for each operation.
VIEWSET_ACTIONS = {CREATE: "create", UPDATE: "partial_update", DELETE: "destroy"}
#: Payload keys that name a new row in the queue, in order of preference.
LABEL_KEYS = ("name", "sku", "code", "key", "slug", "tube_size", "label")


@dataclass(frozen=True)
class ChangeKind:
    key: str
    label: str
    module: str
    #: operation → the registry action applying it needs (``{"UPDATE": "edit", …}``).
    operations: dict[str, str]
    #: The changed model, for UPDATE/DELETE target lookup by ``uid``; None for SET kinds.
    model: type | None
    #: ``(operation, payload, target, context) -> validated data`` (raises the usual validation errors).
    validate: Callable
    #: ``(operation, data, target, user, expected_version) -> the created/updated row or None``.
    apply: Callable
    #: ``(target, payload, context) -> dict`` — what the change replaces, for the reviewer.
    snapshot: Callable
    #: ``(target, payload) -> str`` — a human name for the queue.
    describe: Callable
    #: ``(payload, target, context) -> payload`` — runs on submit before validation; pins what the request was made
    #: against (e.g. the price row it replaces) so approving a stale request is refused rather than overwriting.
    prepare: Callable | None = None


_KINDS: dict[str, ChangeKind] = {}


def register(kind: ChangeKind) -> ChangeKind:
    """Add (or, on a repeated ``ready()``, replace) a kind."""
    _KINDS[kind.key] = kind
    return kind


def get(key: str) -> ChangeKind:
    kind = _KINDS.get(key)
    if kind is None:
        raise DomainError("unknown_change_kind", f"Unknown change kind {key!r}.", errors={"kind": [f"Choose one of: {', '.join(sorted(_KINDS))}."]})
    return kind


def registered() -> list[ChangeKind]:
    return [_KINDS[key] for key in sorted(_KINDS)]


def label_of(key: str) -> str:
    kind = _KINDS.get(key)
    return kind.label if kind else key


def json_safe(value) -> dict:
    """A serializer representation as plain JSON (dates, decimals and uuids as strings)."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def _payload_label(payload: dict) -> str:
    for name in LABEL_KEYS:
        if isinstance(payload, dict) and payload.get(name):
            return str(payload[name])[:255]
    return ""


def viewset_kind(key: str, label: str, viewset, *, model, operations=(CREATE, UPDATE, DELETE)) -> ChangeKind:
    """A kind whose operations are the CRUD actions of ``viewset`` (validated and applied exactly as the endpoint does)."""
    actions = {op: VIEWSET_ACTIONS[op] for op in operations}
    permissions = {op: viewset.action_permissions[action] for op, action in actions.items()}

    def serializer_class(action: str):
        view = viewset()
        view.action = action
        view.request = None
        view.kwargs = {}
        view.format_kwarg = None
        return view.get_serializer_class()

    def validate(operation, payload, target, context):
        if operation == DELETE:
            return {}
        cls = serializer_class(actions[operation])
        body = {name: value for name, value in (payload or {}).items() if name != "expected_version"}
        serializer = cls(target, data=body, partial=True, context=context) if operation == UPDATE else cls(data=body, context=context)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        data.pop("expected_version", None)
        return data

    def apply(operation, data, target, user, expected_version):
        services = viewset.services
        if operation == CREATE:
            return services["create"](user=user, data=data)
        if operation == UPDATE:
            return services["update"](target, user=user, data=data, expected_version=expected_version)
        services["destroy"](target, user=user, expected_version=expected_version)
        return None

    def snapshot(target, payload, context):
        if target is None:
            return {}
        return json_safe(viewset.serializer_class(target, context=context).data)

    def describe(target, payload):
        return (str(target) if target is not None else _payload_label(payload))[:255]

    return register(ChangeKind(key, label, viewset.module, permissions, model, validate, apply, snapshot, describe))
