"""``leads/`` (staff, module ``leads``; record scope all / owned = assigned to me).

CRUD + ``assign/`` (leads.manage), ``status/``, ``mark-lost/``, ``mark-spam/``, ``convert/`` (leads.edit; creating the
customer needs customers.create), ``notes/`` (GET view, POST edit), ``events/`` (cursor).
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.response import Response

from core.errors import NotFound
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from customers.services.customers import visible_customers
from flarize.pagination import CreatedAtCursorPagination, StandardPagination
from leads.filters import LeadFilter
from leads.serializers.staff import (
    AssignSerializer,
    LeadConvertResultSerializer,
    LeadConvertSerializer,
    LeadCreateSerializer,
    LeadEventSerializer,
    LeadLostSerializer,
    LeadNoteCreateSerializer,
    LeadNoteSerializer,
    LeadSerializer,
    LeadSpamSerializer,
    LeadStatusSerializer,
    LeadUpdateSerializer,
)
from leads.services import leads

TAGS = ["leads"]
UUID_RE = "[0-9a-fA-F-]{36}"
_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


class EventCursorPagination(CreatedAtCursorPagination):
    ordering = ("-at", "-id")


def _action_schema(operation_id: str, request, description: str = ""):
    return extend_schema(operation_id=operation_id, request=request, responses={200: LeadSerializer, **_ERRORS}, tags=TAGS, description=description)


@extend_schema_view(
    list=extend_schema(operation_id="leads_list", tags=TAGS),
    retrieve=extend_schema(operation_id="leads_retrieve", responses={200: LeadSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}, tags=TAGS),
    create=extend_schema(operation_id="leads_create", request=LeadCreateSerializer, responses={201: LeadSerializer, **_ERRORS}, tags=TAGS, description="A lead entered in Studio (no OTP)."),
    partial_update=extend_schema(operation_id="leads_update", request=LeadUpdateSerializer, responses={200: LeadSerializer, **_ERRORS}, tags=TAGS),
    destroy=extend_schema(operation_id="leads_delete", responses={204: OpenApiResponse(description="Archived (soft delete)."), **_ERRORS}, tags=TAGS),
)
class LeadViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "leads"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "create": "create",
        "partial_update": "edit",
        "destroy": "archive",
        "assign": "manage",
        "status": "edit",
        "mark_lost": "edit",
        "mark_spam": "edit",
        "convert": "edit",
        "notes": "view",
        "add_note": "edit",
        "events": "view",
    }
    services = {"create": leads.create_lead, "update": leads.update_lead, "destroy": leads.delete_lead}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_RE
    serializer_class = LeadSerializer
    filterset_class = LeadFilter
    search_fields = ["number", "name", "phone_e164", "email"]
    ordering_fields = ["created_at", "updated_at", "status", "number"]
    ordering = ["-created_at"]

    def base_queryset(self):
        return leads.leads_queryset()

    def get_serializer_class(self):
        return {"create": LeadCreateSerializer, "partial_update": LeadUpdateSerializer}.get(self.action, LeadSerializer)

    def _run(self, serializer_class, fn, **extra):
        lead = self.get_object()
        serializer = serializer_class(data=self.request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        updated = fn(lead, user=self.request.user, expected_version=data.pop("expected_version", None), **data, **extra)
        return Response(LeadSerializer(leads.leads_queryset().get(pk=updated.pk)).data)

    @_action_schema("leads_assign", AssignSerializer, "`assignee_uid` null unassigns. Needs leads.manage.")
    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, *args, **kwargs):
        return self._run(AssignSerializer, leads.assign_lead)

    @_action_schema("leads_status", LeadStatusSerializer, "NEW / CONTACTED / QUALIFIED; also reopens a LOST or SPAM lead (→ NEW). 409 `lead_converted`, `invalid_transition`.")
    @action(detail=True, methods=["post"], url_path="status")
    def status(self, request, *args, **kwargs):
        return self._run(LeadStatusSerializer, leads.change_status)

    @_action_schema("leads_mark_lost", LeadLostSerializer, "An open lead → LOST with a reason.")
    @action(detail=True, methods=["post"], url_path="mark-lost")
    def mark_lost(self, request, *args, **kwargs):
        return self._run(LeadLostSerializer, leads.mark_lost)

    @_action_schema("leads_mark_spam", LeadSpamSerializer)
    @action(detail=True, methods=["post"], url_path="mark-spam")
    def mark_spam(self, request, *args, **kwargs):
        return self._run(LeadSpamSerializer, leads.mark_spam)

    @extend_schema(
        operation_id="leads_convert",
        request=LeadConvertSerializer,
        responses={200: LeadConvertResultSerializer, **_ERRORS},
        tags=TAGS,
        description=(
            "Links the lead to `customer_uid`, else to the live customer with the same phone number, else to a new customer (needs customers.create; "
            "owned by the lead's assignee). 409 `lead_already_converted`; 400 `phone_required`. Emits `leads.converted`."
        ),
    )
    @action(detail=True, methods=["post"], url_path="convert")
    def convert(self, request, *args, **kwargs):
        lead = self.get_object()
        serializer = LeadConvertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        customer = None
        if serializer.validated_data.get("customer_uid"):
            customer = visible_customers(request.user).filter(uid=serializer.validated_data["customer_uid"]).first()
            if customer is None:
                raise NotFound(errors={"customer_uid": ["No such customer."]})
        converted, customer, created = leads.convert_lead(lead, user=request.user, customer=customer, expected_version=serializer.validated_data.get("expected_version"))
        body = {"lead": leads.leads_queryset().get(pk=converted.pk), "customer": customer, "customer_created": created}
        return Response(LeadConvertResultSerializer(body).data)

    @extend_schema(operation_id="leads_notes_list", responses={200: LeadNoteSerializer(many=True), 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}, tags=TAGS)
    @action(detail=True, methods=["get"], url_path="notes", filter_backends=[], pagination_class=StandardPagination)
    def notes(self, request, *args, **kwargs):
        page = self.paginate_queryset(leads.notes_queryset(self.get_object()))
        return self.get_paginated_response(LeadNoteSerializer(page, many=True).data)

    @extend_schema(operation_id="leads_notes_create", request=LeadNoteCreateSerializer, responses={201: LeadNoteSerializer, **_ERRORS}, tags=TAGS)
    @notes.mapping.post
    def add_note(self, request, *args, **kwargs):
        lead = self.get_object()
        serializer = LeadNoteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = leads.add_note(lead, user=request.user, body=serializer.validated_data["body"])
        return Response(LeadNoteSerializer(note).data, status=http_status.HTTP_201_CREATED)

    @extend_schema(operation_id="leads_events_list", responses={200: LeadEventSerializer(many=True), 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}, tags=TAGS)
    @action(detail=True, methods=["get"], url_path="events", filter_backends=[], pagination_class=EventCursorPagination)
    def events(self, request, *args, **kwargs):
        page = self.paginate_queryset(leads.events_queryset(self.get_object()))
        return self.get_paginated_response(LeadEventSerializer(page, many=True).data)
