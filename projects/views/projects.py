"""``projects/`` — CRUD, ``lock-bom/``, ``cost-inputs/``, ``commission/``, ``close/``, ``cancel/`` (module ``projects``, scope all)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.errors import PermissionDenied
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from pricing.services.common import can_see_internal
from projects.filters import ProjectFilter
from projects.serializers.projects import (
    CloseSerializer,
    CommissionSerializer,
    CostInputsSerializer,
    LockBomSerializer,
    ProjectCancelSerializer,
    ProjectCreateSerializer,
    ProjectDetailSerializer,
    ProjectSerializer,
    ProjectUpdateSerializer,
)
from projects.services import bom_lock, projects

TAGS = ["projects"]
UUID_RE = "[0-9a-fA-F-]{36}"
_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


def _action_schema(name: str, request, description: str):
    return extend_schema(operation_id=f"projects_{name}", request=request, responses={200: ProjectDetailSerializer, **_ERRORS}, tags=TAGS, description=description)


@extend_schema_view(
    list=extend_schema(operation_id="projects_list", tags=TAGS),
    retrieve=extend_schema(operation_id="projects_retrieve", responses={200: ProjectDetailSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}, tags=TAGS),
    create=extend_schema(
        operation_id="projects_create",
        request=ProjectCreateSerializer,
        responses={201: ProjectDetailSerializer, **_ERRORS},
        tags=TAGS,
        description="A PLANNED project (number PROJ-<n>). 409 `inspection_has_project` when a live project already exists for the site inspection.",
    ),
    partial_update=extend_schema(
        operation_id="projects_update",
        request=ProjectUpdateSerializer,
        responses={200: ProjectDetailSerializer, **_ERRORS},
        tags=TAGS,
        description="409 `bom_locked` for the customer or system fields after the BOM lock, `project_finished` on a closed/cancelled project.",
    ),
    destroy=extend_schema(
        operation_id="projects_delete",
        responses={204: OpenApiResponse(description="Archived (soft delete)."), **_ERRORS},
        tags=TAGS,
        description="Only a PLANNED or CANCELLED project (409 `project_not_deletable`).",
    ),
)
class ProjectViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "projects"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "create": "create",
        "partial_update": "edit",
        "destroy": "archive",
        "lock_bom": "lock",
        "cost_inputs": "edit",
        "commission": "edit",
        "close": "edit",
        "cancel": "archive",
    }
    services = {"create": projects.create_project, "update": projects.update_project, "destroy": projects.delete_project}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_RE
    serializer_class = ProjectSerializer
    filterset_class = ProjectFilter
    search_fields = ["number", "title", "customer__name", "customer__code"]
    ordering_fields = ["number", "created_at", "updated_at", "scheduled_on", "status"]
    ordering = ["-created_at"]

    def base_queryset(self):
        queryset = projects.projects_queryset()
        return queryset.defer("bom_lock", "cost_inputs") if self.action == "list" else queryset

    def get_serializer_class(self):
        return {"list": ProjectSerializer, "create": ProjectCreateSerializer, "partial_update": ProjectUpdateSerializer}.get(self.action, ProjectDetailSerializer)

    def _detail(self, project, status=200):
        return Response(ProjectDetailSerializer(projects.projects_queryset().get(pk=project.pk), context=self.get_serializer_context()).data, status=status)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return self._detail(serializer.instance, status=201)

    def partial_update(self, request, *args, **kwargs):
        serializer = self.get_serializer(self.get_object(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return self._detail(serializer.instance)

    def _validated(self, serializer_class):
        serializer = serializer_class(data=self.request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        return data, data.pop("expected_version", None)

    @_action_schema(
        "lock_bom",
        LockBomSerializer,
        "Runs the engineering checker on the lines (stored as an engineering run either way) and locks the BOM: PLANNED → IN_PROGRESS. "
        "409 `bom_lock_refused` lists unwaived BLOCK findings (engineering waives them under engineering/findings/<uid>/acknowledge/) and "
        "warnings that need an acknowledgement; `bom_already_locked`, `no_active_rule_set`.",
    )
    @action(detail=True, methods=["post"], url_path="lock-bom")
    def lock_bom(self, request, *args, **kwargs):
        project = self.get_object()
        data, expected = self._validated(LockBomSerializer)
        locked = bom_lock.lock_bom(project, user=request.user, architecture=data["architecture"], lines=data["lines"], acknowledgements=data["acknowledgements"], expected_version=expected)
        return self._detail(locked)

    @_action_schema("cost_inputs", CostInputsSerializer, "Replaces the cost inputs (needs pricing_internal.view too). 409 `project_not_open` once commissioned/closed/cancelled.")
    @action(detail=True, methods=["post"], url_path="cost-inputs")
    def cost_inputs(self, request, *args, **kwargs):
        if not can_see_internal(request.user):
            raise PermissionDenied("permission_denied", "Cost inputs need the pricing_internal view permission.")
        project = self.get_object()
        data, expected = self._validated(CostInputsSerializer)
        return self._detail(projects.set_cost_inputs(project, user=request.user, inputs=data, expected_version=expected))

    @_action_schema("commission", CommissionSerializer, "IN_PROGRESS → COMMISSIONED (409 `invalid_transition` otherwise).")
    @action(detail=True, methods=["post"], url_path="commission")
    def commission(self, request, *args, **kwargs):
        project = self.get_object()
        data, expected = self._validated(CommissionSerializer)
        return self._detail(projects.commission(project, user=request.user, expected_version=expected, **data))

    @_action_schema("close", CloseSerializer, "COMMISSIONED → CLOSED (409 `invalid_transition` otherwise).")
    @action(detail=True, methods=["post"], url_path="close")
    def close(self, request, *args, **kwargs):
        project = self.get_object()
        data, expected = self._validated(CloseSerializer)
        return self._detail(projects.close(project, user=request.user, note=data["note"], expected_version=expected))

    @_action_schema("cancel", ProjectCancelSerializer, "PLANNED/IN_PROGRESS → CANCELLED with a reason (409 `invalid_transition` otherwise).")
    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, *args, **kwargs):
        project = self.get_object()
        data, expected = self._validated(ProjectCancelSerializer)
        return self._detail(projects.cancel(project, user=request.user, reason=data["reason"], expected_version=expected))
