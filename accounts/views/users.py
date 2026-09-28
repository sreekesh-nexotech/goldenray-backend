"""``users/`` — staff accounts (module ``users``).

``users.view`` list/detail · ``users.create`` create (invitation e-mail, no password) · ``users.edit`` profile
fields (e-mail and role also need ``users.manage``) · ``users.archive`` deactivate/reactivate/delete ·
``users.manage`` force a password reset. Escalation guards live in ``accounts.services.users``.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.filters import UserFilter
from accounts.serializers.users import UserActionSerializer, UserCreateSerializer, UserSerializer, UserUpdateSerializer
from accounts.services import users
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin

TAGS = ["users"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
_WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


@extend_schema_view(
    list=extend_schema(operation_id="users_list", tags=TAGS),
    retrieve=extend_schema(operation_id="users_retrieve", responses={200: UserSerializer, 404: ErrorSerializer}, tags=TAGS),
    create=extend_schema(operation_id="users_create", request=UserCreateSerializer, responses={201: UserSerializer, **_WRITE_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="users_update", request=UserUpdateSerializer, responses={200: UserSerializer, **_WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(operation_id="users_delete", responses={204: OpenApiResponse(description="Deleted."), **_WRITE_ERRORS}, tags=TAGS),
)
class UserViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "users"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "create": "create",
        "partial_update": "edit",
        "destroy": "archive",
        "deactivate": "archive",
        "reactivate": "archive",
        "force_reset": "manage",
    }
    services = {"create": users.create_user, "update": users.update_user, "destroy": users.delete_user}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = UserSerializer
    filterset_class = UserFilter
    search_fields = ["email", "first_name", "last_name", "title"]
    ordering_fields = ["email", "first_name", "last_name", "created_at", "last_login_at"]
    ordering = ["email"]

    def base_queryset(self):
        return users.users_queryset()

    def get_serializer_class(self):
        return {"create": UserCreateSerializer, "partial_update": UserUpdateSerializer}.get(self.action, UserSerializer)

    def _run(self, request, service):
        instance = self.get_object()
        serializer = UserActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        updated = service(instance, user=request.user, expected_version=data.get("expected_version"), note=data.get("note", ""))
        return Response(UserSerializer(updated, context=self.get_serializer_context()).data)

    @extend_schema(operation_id="users_deactivate", request=UserActionSerializer, responses={200: UserSerializer, **_WRITE_ERRORS}, tags=TAGS, description="Signs out every session.")
    @action(detail=True, methods=["post"])
    def deactivate(self, request, *args, **kwargs):
        return self._run(request, users.deactivate_user)

    @extend_schema(operation_id="users_reactivate", request=UserActionSerializer, responses={200: UserSerializer, **_WRITE_ERRORS}, tags=TAGS)
    @action(detail=True, methods=["post"])
    def reactivate(self, request, *args, **kwargs):
        return self._run(request, users.reactivate_user)

    @extend_schema(
        operation_id="users_force_reset",
        request=UserActionSerializer,
        responses={200: UserSerializer, **_WRITE_ERRORS},
        tags=TAGS,
        description="Invalidates the password, signs out every session and e-mails a set-password link.",
    )
    @action(detail=True, methods=["post"], url_path="force-reset")
    def force_reset(self, request, *args, **kwargs):
        return self._run(request, users.force_password_reset)
