"""``customers/<uid>/notes/`` (list, add) and ``customers/<uid>/notes/<note_uid>/`` (edit/pin, delete).

The parent customer is resolved through the ``customers`` record scope first (404 when not visible); note rows are
scoped through their customer's owner as well.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.response import Response

from core.errors import NotFound
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, DestroyModelMixin, ListModelMixin, UpdateModelMixin
from customers.serializers.customers import CustomerNoteCreateSerializer, CustomerNoteSerializer, CustomerNoteUpdateSerializer
from customers.services import customers, notes

TAGS = ["customers"]
_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


@extend_schema_view(
    list=extend_schema(operation_id="customers_notes_list", tags=TAGS),
    create=extend_schema(operation_id="customers_notes_create", request=CustomerNoteCreateSerializer, responses={201: CustomerNoteSerializer, **_ERRORS}, tags=TAGS),
    partial_update=extend_schema(operation_id="customers_notes_update", request=CustomerNoteUpdateSerializer, responses={200: CustomerNoteSerializer, **_ERRORS}, tags=TAGS),
    destroy=extend_schema(operation_id="customers_notes_delete", responses={204: OpenApiResponse(description="Deleted."), **_ERRORS}, tags=TAGS),
)
class CustomerNoteViewSet(ListModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "customers"
    action_permissions = {"list": "view", "create": "edit", "partial_update": "edit", "destroy": "edit"}
    services = {"update": notes.update_note, "destroy": notes.delete_note}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_field = "uid"
    lookup_url_kwarg = "note_uid"
    lookup_value_regex = "[0-9a-fA-F-]{36}"
    serializer_class = CustomerNoteSerializer
    filter_backends: list = []

    def customer(self):
        found = customers.visible_customers(self.request.user).filter(uid=self.kwargs["customer_uid"]).first()
        if found is None:
            raise NotFound("No such customer.")
        return found

    def base_queryset(self):
        return notes.notes_queryset(self.customer())

    def get_serializer_class(self):
        return {"create": CustomerNoteCreateSerializer, "partial_update": CustomerNoteUpdateSerializer}.get(self.action, CustomerNoteSerializer)

    def create(self, request, *args, **kwargs):
        customer = self.customer()
        serializer = CustomerNoteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = notes.add_note(customer, user=request.user, **serializer.validated_data)
        return Response(CustomerNoteSerializer(note).data, status=status.HTTP_201_CREATED)
