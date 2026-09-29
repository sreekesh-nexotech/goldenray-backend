"""``inventory/movements/`` (list + POST, append-only) and ``GET inventory/balances/`` (module ``inventory``; flag
``INVENTORY_STOCK``, 404 while off).

Movements are never edited or deleted: there is no detail, PATCH or DELETE route. A mistake is corrected by a new
movement (an ADJUST with a note). An OUT that would take a balance below zero is refused with 409
``insufficient_stock`` unless it is an ADJUST (which always carries a note) by a holder of ``inventory.edit``.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, extend_schema_view

from core.flags import FlagRequiredMixin
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, ListModelMixin
from inventory.filters import BalanceFilter, MovementFilter
from inventory.serializers.movements import BalanceSerializer, MovementCreateSerializer, MovementRecordedSerializer, MovementSerializer
from inventory.services import balances, movements
from inventory.services.common import FLAG

TAGS = ["inventory"]
FLAG_NOTE = "Answers 404 while the INVENTORY_STOCK flag is off."


@extend_schema_view(
    list=extend_schema(operation_id="inventory_movements_list", tags=TAGS, description=f"The stock ledger, newest first. {FLAG_NOTE}"),
    create=extend_schema(
        operation_id="inventory_movements_create",
        request=MovementCreateSerializer,
        responses={201: MovementRecordedSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer},
        tags=TAGS,
        description=(
            "Append one movement. 409 `insufficient_stock` when an OUT would take the balance below zero, unless the reason is ADJUST "
            "(an ADJUST always carries a note) — then it is booked and audited as a negative-stock override."
        ),
    ),
)
class MovementViewSet(FlagRequiredMixin, ListModelMixin, CreateModelMixin, BaseViewSet):
    required_flag = FLAG
    module = "inventory"
    action_permissions = {"list": "view", "create": "edit"}
    services = {"create": movements.record_movement}
    http_method_names = ["get", "post"]
    serializer_class = MovementSerializer
    filterset_class = MovementFilter
    search_fields = ["component__sku", "component__name", "location__code", "note"]
    ordering_fields = ["at", "created_at", "qty"]
    ordering = ["-at", "-id"]

    def get_serializer_class(self):
        return MovementCreateSerializer if self.action == "create" else MovementSerializer

    def base_queryset(self):
        return movements.movements_queryset()


@extend_schema_view(
    list=extend_schema(
        operation_id="inventory_balances_list",
        tags=TAGS,
        description=f"Stock per component and location (Σ IN − Σ OUT over the ledger), filtered by `component` and/or `location`. {FLAG_NOTE}",
    ),
)
class BalanceViewSet(FlagRequiredMixin, ListModelMixin, BaseViewSet):
    required_flag = FLAG
    module = "inventory"
    action_permissions = {"list": "view"}
    http_method_names = ["get"]
    serializer_class = BalanceSerializer
    filterset_class = BalanceFilter
    search_fields = ["component__sku", "component__name", "location__code", "location__name"]
    ordering_fields = ["qty", "last_movement_at"]
    ordering = ["component__sku", "location__code"]

    def base_queryset(self):
        return balances.balances_queryset()
