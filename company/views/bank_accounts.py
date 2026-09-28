"""``company/bank-accounts/`` — CRUD + ``make-primary/`` (module ``company``: view / edit)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from company.serializers.bank_accounts import BankAccountActionSerializer, BankAccountCreateSerializer, BankAccountSerializer, BankAccountUpdateSerializer
from company.services import bank_accounts
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin

TAGS = ["company"]
_WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


@extend_schema_view(
    list=extend_schema(operation_id="company_bank_accounts_list", tags=TAGS),
    retrieve=extend_schema(operation_id="company_bank_accounts_retrieve", responses={200: BankAccountSerializer, 404: ErrorSerializer}, tags=TAGS),
    create=extend_schema(operation_id="company_bank_accounts_create", request=BankAccountCreateSerializer, responses={201: BankAccountSerializer, **_WRITE_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="company_bank_accounts_update", request=BankAccountUpdateSerializer, responses={200: BankAccountSerializer, **_WRITE_ERRORS}, tags=TAGS),
    destroy=extend_schema(operation_id="company_bank_accounts_delete", responses={204: OpenApiResponse(description="Deleted."), **_WRITE_ERRORS}, tags=TAGS),
)
class BankAccountViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "company"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "create": "edit",
        "partial_update": "edit",
        "destroy": "edit",
        "make_primary": "edit",
    }
    services = {"create": bank_accounts.create_account, "update": bank_accounts.update_account, "destroy": bank_accounts.delete_account}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = "[0-9a-fA-F-]{36}"
    serializer_class = BankAccountSerializer
    search_fields = ["label", "bank", "account_name", "ifsc"]
    ordering_fields = ["label", "created_at"]
    ordering = ["-is_primary", "label"]

    def base_queryset(self):
        return bank_accounts.accounts_queryset()

    def get_serializer_class(self):
        return {"create": BankAccountCreateSerializer, "partial_update": BankAccountUpdateSerializer}.get(self.action, BankAccountSerializer)

    @extend_schema(operation_id="company_bank_accounts_make_primary", request=BankAccountActionSerializer, responses={200: BankAccountSerializer, **_WRITE_ERRORS}, tags=TAGS)
    @action(detail=True, methods=["post"], url_path="make-primary")
    def make_primary(self, request, *args, **kwargs):
        account = self.get_object()
        serializer = BankAccountActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = bank_accounts.make_primary(account, user=request.user, expected_version=serializer.validated_data.get("expected_version"))
        return Response(BankAccountSerializer(updated).data)
