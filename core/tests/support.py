"""Views used only by the core test-suite (mounted by core/tests/urls_testing.py)."""

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404, JsonResponse
from rest_framework import exceptions, serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.errors import Conflict, DomainError, NotFound, PermissionDenied, StaleVersion
from core.flags import FlagRequiredMixin, require_flag
from core.idempotency import idempotent
from core.models import FeatureFlag
from core.views import AgentAPIView, BaseAPIView, BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, PublicAPIView, RetrieveModelMixin, UpdateModelMixin
from flarize.cache_utils import cache_response

SERVICE_CALLS: list = []


class OpenView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]


class EchoVersionView(OpenView):
    def get(self, request, *args, **kwargs):
        return Response({"version": request.version})


class PublicEchoView(PublicAPIView):
    def get(self, request, *args, **kwargs):
        return Response({"version": request.version})


class InputSerializer(serializers.Serializer):
    email = serializers.EmailField()
    age = serializers.IntegerField(min_value=18)


class RaiseView(APIView):
    """GET /raise/<kind>/ raises the named exception; POST validates InputSerializer. Uses the real JWT authenticator."""

    permission_classes = [AllowAny]

    def get(self, request, kind, *args, **kwargs):
        errors = {
            "domain": lambda: DomainError("quota_exceeded", "Quota exceeded.", status=422, errors={"lines": ["Too many lines."]}),
            "not_found": lambda: NotFound(),
            "conflict": lambda: Conflict(),
            "denied": lambda: PermissionDenied(),
            "stale": lambda: StaleVersion(),
            "drf_denied": lambda: exceptions.PermissionDenied(),
            "django_denied": lambda: DjangoPermissionDenied(),
            "http404": lambda: Http404("gone"),
            "not_authenticated": lambda: exceptions.NotAuthenticated(),
            "auth_failed": lambda: exceptions.AuthenticationFailed("Bad token."),
            "throttled": lambda: exceptions.Throttled(wait=12.3),
            "django_validation": lambda: DjangoValidationError({"name": ["Name is taken."]}),
            "django_validation_list": lambda: DjangoValidationError(["Something is wrong."]),
            "non_field": lambda: exceptions.ValidationError(["Dates overlap."]),
            "nested": lambda: exceptions.ValidationError({"lines": [{"qty": [exceptions.ErrorDetail("Too small.", code="min_value")]}]}),
            "unexpected": lambda: RuntimeError("secret internal detail 42"),
            "custom_api": lambda: exceptions.APIException("Service unavailable.", code="upstream_down"),
        }
        raise errors[kind]()

    def post(self, request, *args, **kwargs):
        serializer = InputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_201_CREATED)


class ThingsView(PublicAPIView):
    """Public cached endpoint depending on two namespaces."""

    calls = 0

    @cache_response(namespaces=["tests:things", "tests:other"], ttl=120)
    def get(self, request, *args, **kwargs):
        ThingsView.calls += 1
        if request.query_params.get("fail"):
            return Response({"code": "nope"}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"items": [1, 2, 3], "calls": ThingsView.calls})

    def post(self, request, *args, **kwargs):
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


class NoNamespaceView(PublicAPIView):
    @cache_response(namespaces=[])
    def get(self, request, *args, **kwargs):
        return Response({})


class IdempotentView(PublicAPIView):
    calls = 0

    @idempotent("tests.leads")
    def post(self, request, *args, **kwargs):
        IdempotentView.calls += 1
        if request.data.get("explode"):
            return Response({"code": "server_error"}, status=500)
        return Response({"received": request.data.get("name"), "call": IdempotentView.calls}, status=status.HTTP_201_CREATED)


class RequiredIdempotentView(PublicAPIView):
    @idempotent("tests.required", required=True)
    def post(self, request, *args, **kwargs):
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


class OtpLikeView(PublicAPIView):
    """Throttled per phone number (like the OTP endpoint) instead of per IP."""

    throttle_scope = "otp"

    def get_throttle_ident(self, request):
        return f"phone:{request.data.get('phone', '')}"

    def post(self, request, *args, **kwargs):
        return Response({"sent": True})


class AgentPingView(AgentAPIView):
    def get(self, request, *args, **kwargs):
        return Response({"principal": str(request.user), "kind": request.user.kind})


class GatedView(FlagRequiredMixin, BaseAPIView):
    required_flag = "LEGACY_API_SHIM"
    module = "settings"
    action_permissions = {"GET": "view"}

    def get(self, request, *args, **kwargs):
        return Response({"gated": True})


@require_flag("ADMS_RECEIVER")
def plain_gated_view(request, device_token):
    return JsonResponse({"gated": True})


def plain_boom_view(request, device_token):
    """A plain Django view (like the /iclock/ receiver) that fails unexpectedly."""
    raise RuntimeError("plain view failure")


class StaffMethodView(BaseAPIView):
    module = "settings"
    action_permissions = {"GET": "view", "POST": ("audit", "view")}

    def get(self, request, *args, **kwargs):
        return Response({"ok": True})

    def post(self, request, *args, **kwargs):
        return Response({"ok": True})

    def delete(self, request, *args, **kwargs):
        return Response({"ok": True})


class FlagRowSerializer(serializers.ModelSerializer):
    expected_version = serializers.IntegerField(min_value=1, required=False, write_only=True)

    class Meta:
        model = FeatureFlag
        fields = ["uid", "key", "enabled", "version", "expected_version"]
        read_only_fields = ["uid", "version"]


def _create(*, user, data):
    SERVICE_CALLS.append(("create", user, data))
    return FeatureFlag.objects.create(created_by=user, updated_by=user, **data)


def _update(instance, *, user, data, expected_version):
    SERVICE_CALLS.append(("update", user, data, expected_version))
    from core.services.versioning import save_versioned

    for key, value in data.items():
        setattr(instance, key, value)
    return save_versioned(instance, user=user, fields=list(data), expected_version=expected_version)


def _destroy(instance, *, user, expected_version):
    SERVICE_CALLS.append(("destroy", user, expected_version))
    from core.services.versioning import check_version

    check_version(instance, expected_version)
    instance.soft_delete(user)


class FlagRowViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "settings"
    record_scope_module = "customers"
    serializer_class = FlagRowSerializer
    action_permissions = {"list": "view", "retrieve": "view", "create": "edit", "partial_update": "edit", "destroy": "edit"}
    services = {"create": _create, "update": _update, "destroy": _destroy}
    http_method_names = ["get", "post", "patch", "delete"]

    def base_queryset(self):
        return FeatureFlag.objects.order_by("id")


class UnmappedViewSet(ListModelMixin, BaseViewSet):
    module = "settings"
    serializer_class = FlagRowSerializer
    action_permissions: dict = {}

    def base_queryset(self):
        return FeatureFlag.objects.all()
