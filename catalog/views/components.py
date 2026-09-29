"""``catalog/components/`` — CRUD, lifecycle actions, history and usage (module ``catalog``; PLAN §3.4 Catalog).

``catalog.view`` list/detail/history/usage · ``catalog.create`` create · ``catalog.edit`` edit (incl. the nested spec
and tiers) · ``catalog.approve`` activate/deprecate/retire · ``catalog.archive`` delete (409 ``component_in_use``
while referenced). Import/export live in ``catalog.views.component_io``.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from catalog.filters import ComponentFilter
from catalog.serializers.components import (
    ComponentChangeSerializer,
    ComponentSerializer,
    ComponentUpdateSerializer,
    ComponentWriteSerializer,
    DeprecateSerializer,
    LifecycleActionSerializer,
    UsageSerializer,
)
from catalog.services import components, lifecycle, usage
from catalog.services.history import history_queryset
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from flarize.pagination import CreatedAtCursorPagination

TAGS = ["catalog"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
_WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


class ChangeCursorPagination(CreatedAtCursorPagination):
    ordering = ("-at", "-id")


@extend_schema_view(
    list=extend_schema(operation_id="catalog_components_list", tags=TAGS),
    retrieve=extend_schema(operation_id="catalog_components_retrieve", responses={200: ComponentSerializer, 404: ErrorSerializer}, tags=TAGS),
    create=extend_schema(
        operation_id="catalog_components_create",
        request=ComponentWriteSerializer,
        responses={201: ComponentSerializer, **_WRITE_ERRORS},
        tags=TAGS,
        description="Creates a DRAFT component. Send the spec matching the category's `spec_kind` (`panel_spec`, `inverter_spec`, `battery_spec` or `structure_spec`).",
    ),
    partial_update=extend_schema(operation_id="catalog_components_update", request=ComponentUpdateSerializer, responses={200: ComponentSerializer, **_WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(
        operation_id="catalog_components_delete",
        responses={204: OpenApiResponse(description="Deleted (soft)."), **_WRITE_ERRORS},
        tags=TAGS,
        description="Refused with 409 `component_in_use` while anything references the component (see `usage/`); retire it instead.",
    ),
)
class ComponentViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "catalog"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "history": "view",
        "usage": "view",
        "create": "create",
        "partial_update": "edit",
        "destroy": "archive",
        "activate": "approve",
        "deprecate": "approve",
        "retire": "approve",
    }
    services = {"create": components.create_component, "update": components.update_component, "destroy": components.delete_component}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = ComponentSerializer
    filterset_class = ComponentFilter
    ordering_fields = ["sku", "name", "status", "created_at", "updated_at"]
    ordering = ["sku"]

    def base_queryset(self):
        return components.components_queryset()

    def get_serializer_class(self):
        return {"create": ComponentWriteSerializer, "partial_update": ComponentUpdateSerializer}.get(self.action, ComponentSerializer)

    def _respond(self, component):
        return Response(ComponentSerializer(components.components_queryset().get(pk=component.pk), context=self.get_serializer_context()).data)

    @extend_schema(operation_id="catalog_components_activate", request=LifecycleActionSerializer, responses={200: ComponentSerializer, **_WRITE_ERRORS}, tags=TAGS)
    @action(detail=True, methods=["post"])
    def activate(self, request, *args, **kwargs):
        body = LifecycleActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        component = lifecycle.activate(self.get_object(), user=request.user, expected_version=body.validated_data.get("expected_version"), reason=body.validated_data.get("reason", ""))
        return self._respond(component)

    @extend_schema(
        operation_id="catalog_components_deprecate",
        request=DeprecateSerializer,
        responses={200: ComponentSerializer, **_WRITE_ERRORS},
        tags=TAGS,
        description="ACTIVE → DEPRECATED with a reason and an optional replacement of the same category.",
    )
    @action(detail=True, methods=["post"])
    def deprecate(self, request, *args, **kwargs):
        body = DeprecateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        component = lifecycle.deprecate(self.get_object(), user=request.user, reason=data["reason"], replacement=data.get("replacement_uid"), expected_version=data.get("expected_version"))
        return self._respond(component)

    @extend_schema(
        operation_id="catalog_components_retire",
        request=LifecycleActionSerializer,
        responses={200: ComponentSerializer, **_WRITE_ERRORS},
        tags=TAGS,
        description="Terminal: a RETIRED component can never be selected in a new pack, BOM or quotation.",
    )
    @action(detail=True, methods=["post"])
    def retire(self, request, *args, **kwargs):
        body = LifecycleActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        component = lifecycle.retire(self.get_object(), user=request.user, reason=body.validated_data.get("reason", ""), expected_version=body.validated_data.get("expected_version"))
        return self._respond(component)

    @extend_schema(operation_id="catalog_components_history", responses={200: ComponentChangeSerializer(many=True), 404: ErrorSerializer}, tags=TAGS)
    # filter_backends=[]: the component list filters do not apply to a change log (nor may they 404 the component).
    @action(detail=True, methods=["get"], pagination_class=ChangeCursorPagination, filter_backends=[])
    def history(self, request, *args, **kwargs):
        paginator = ChangeCursorPagination()
        # view=None: the cursor orders by (-at, -id), not by the component list's ordering filter.
        page = paginator.paginate_queryset(history_queryset(self.get_object()), request, view=None)
        return paginator.get_paginated_response(ComponentChangeSerializer(page, many=True).data)

    @extend_schema(operation_id="catalog_components_usage", responses={200: UsageSerializer, 404: ErrorSerializer}, tags=TAGS)
    @action(detail=True, methods=["get"], pagination_class=None)
    def usage(self, request, *args, **kwargs):
        component = self.get_object()
        sections = usage.usage_of(component)
        total = sum(section.count for section in sections)
        payload = {
            "component": component,
            "in_use": bool(total) or any(section.error for section in sections),
            "total": total,
            "sections": [section.__dict__ for section in sections],
        }
        return Response(UsageSerializer(payload).data)
