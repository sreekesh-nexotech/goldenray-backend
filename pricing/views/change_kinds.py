"""Pricing changes a Project Head asks for through a change request (``approvals``).

* ``pricing.cost_config`` (SET) — the ``PUT pricing/cost-config/`` body (cost rates and the tariff); applied with
  :func:`pricing.services.cost_config.put_values`, whose per-key ``current_uid`` check refuses a stale request.
* the installation matrix and statutory fees — the CRUD endpoints of :mod:`pricing.views.config`.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from django.core.exceptions import ValidationError

from approvals.services.kinds import CREATE, SET, ChangeKind, json_safe, register, viewset_kind
from catalog.models import Component
from core.errors import PermissionDenied
from pricing.models import InstallationMatrix, Price, StatutoryFee
from pricing.serializers.config import CostConfigPutSerializer, CostConfigRowSerializer
from pricing.serializers.prices import PriceRowSerializer
from pricing.services import cost_config
from pricing.services.common import can_see_internal
from pricing.views import config
from pricing.views import prices as prices_views


def _user(context):
    request = (context or {}).get("request")
    return getattr(request, "user", None)


def _validate_cost_config(operation, payload, target, context):
    body = CostConfigPutSerializer(data=payload)
    body.is_valid(raise_exception=True)
    data = body.validated_data
    entries = [dict(entry) for entry in data["entries"]]
    user = _user(context)
    # The PUT endpoint's own rule: margin keys need pricing_internal, for whoever submits or applies them.
    if not can_see_internal(user) and any(cost_config.KEYS[entry["key"]].internal or entry["key"] == "cost_engine.config" for entry in entries):
        raise PermissionDenied("pricing_internal_required", "Margin configuration needs the pricing_internal permission.")
    for entry in entries:
        cost_config.validate_value(entry["key"], entry.get("value"))
    return {"entries": entries, "effective_from": data.get("effective_from"), "note": data.get("note", "")}


def _apply_cost_config(operation, data, target, user, expected_version):
    cost_config.put_values(user=user, entries=data["entries"], effective_from=data.get("effective_from"), note=data.get("note", ""))
    return None


def _snapshot_cost_config(target, payload, context):
    keys = {entry.get("key") for entry in (payload or {}).get("entries") or [] if isinstance(entry, dict)}
    rows = [row for key, row in cost_config.current_rows().items() if key in keys]
    serializer_context = {**(context or {}), "internal": can_see_internal(_user(context))}
    return {"current": json_safe(CostConfigRowSerializer(rows, many=True, context=serializer_context).data)}


def _describe_cost_config(target, payload):
    keys = [str(entry.get("key")) for entry in (payload or {}).get("entries") or [] if isinstance(entry, dict)]
    return f"Cost configuration: {', '.join(keys)}"[:255]


def register_kinds() -> None:
    register(
        ChangeKind(
            key="pricing.cost_config",
            label="Cost rates / tariff",
            module="pricing",
            operations={SET: "edit"},
            model=None,
            validate=_validate_cost_config,
            apply=_apply_cost_config,
            snapshot=_snapshot_cost_config,
            describe=_describe_cost_config,
        )
    )
    viewset_kind("pricing.installation_matrix", "Installation rate", config.InstallationMatrixViewSet, model=InstallationMatrix)
    viewset_kind("pricing.statutory_fee", "Statutory fee", config.StatutoryFeeViewSet, model=StatutoryFee)
    register_price_kind()


# ── manual component prices (POST pricing/prices/) ─────────────────────────────────────────────────────────────────
# The BOM quote reads the current LIST row of every component directly, so a manual price is live the moment it is
# written: it needs the same approval as the rates above. LANDED rows are internal cost data (`pricing_internal`).


def _current_price(payload: dict):
    try:
        return Price.objects.filter(component__uid=payload.get("component_uid"), kind=payload.get("kind"), effective_to__isnull=True).select_related("component").first()
    except (ValueError, TypeError, ValidationError):  # a malformed uid is the serializer's 400 to report
        return None


def _prepare_price(payload, target, context):
    """Pin the row being replaced, so approving after someone else re-priced the component is a 409, not an overwrite."""
    if "expected_current_uid" in payload:
        return payload
    current = _current_price(payload)
    return {**payload, "expected_current_uid": str(current.uid) if current else None}


def _snapshot_price(target, payload, context):
    row = _current_price(payload or {})
    if row is None:
        return {}
    serializer_context = {**(context or {}), "internal": can_see_internal(_user(context))}
    return json_safe(PriceRowSerializer(row, context=serializer_context).data)


def _describe_price(target, payload):
    component = Component.objects.filter(uid=(payload or {}).get("component_uid")).first() if _is_uuid((payload or {}).get("component_uid")) else None
    return f"{component.sku if component else 'Component'} — {(payload or {}).get('kind', '')} price"[:255]


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def register_price_kind() -> None:
    kind = viewset_kind("pricing.price", "Component price", prices_views.PriceViewSet, model=Price, operations=(CREATE,))
    register(replace(kind, prepare=_prepare_price, snapshot=_snapshot_price, describe=_describe_price))
