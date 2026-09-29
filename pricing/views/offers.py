"""``pricing/offers/`` — CRUD + ``approve/``, ``activate/``, ``pause/``, ``archive/`` (module ``offers``).

``offers.view`` reads · ``create`` · ``edit`` (DRAFT/APPROVED only) · ``approve`` (DRAFT → APPROVED) · ``publish``
(activate / pause) · ``archive`` (archive; DELETE of a DRAFT).
"""

from __future__ import annotations

import django_filters
from django.db.models import Q
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.views import BaseViewSet, CreateModelMixin, DestroyModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from pricing.models import Offer, OfferStatus, OfferSystem, OfferTier
from pricing.serializers.common import ReasonSerializer
from pricing.serializers.offers import OfferSerializer, OfferUpdateSerializer, OfferWriteSerializer
from pricing.services import offers
from pricing.views.common import READ_ERRORS, TAGS, UUID_REGEX, WRITE_ERRORS


class OfferFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=OfferStatus.choices)
    applies_to_system = django_filters.ChoiceFilter(choices=OfferSystem.choices)
    applies_to_tier = django_filters.ChoiceFilter(choices=OfferTier.choices)
    active_on = django_filters.DateFilter(method="filter_active_on", help_text="ACTIVE offers whose dates cover this day.")

    class Meta:
        model = Offer
        fields: list[str] = []

    def filter_active_on(self, queryset, name, value):
        return queryset.filter(status=OfferStatus.ACTIVE).filter(Q(starts_on__isnull=True) | Q(starts_on__lte=value)).filter(Q(ends_on__isnull=True) | Q(ends_on__gte=value))


def _transition_schema(name: str, description: str):
    return extend_schema(operation_id=f"pricing_offers_{name}", request=ReasonSerializer, responses={200: OfferSerializer, **WRITE_ERRORS}, tags=TAGS, description=description)


@extend_schema_view(
    list=extend_schema(operation_id="pricing_offers_list", tags=TAGS),
    retrieve=extend_schema(operation_id="pricing_offers_retrieve", responses={200: OfferSerializer, **READ_ERRORS}, tags=TAGS),
    create=extend_schema(operation_id="pricing_offers_create", request=OfferWriteSerializer, responses={201: OfferSerializer, **WRITE_ERRORS}, tags=TAGS, description="Creates a DRAFT offer."),
    partial_update=extend_schema(
        operation_id="pricing_offers_update",
        request=OfferUpdateSerializer,
        responses={200: OfferSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="DRAFT/APPROVED only; a content change bumps content_version and sends an APPROVED offer back to DRAFT.",
    ),
    destroy=extend_schema(operation_id="pricing_offers_delete", responses={204: OpenApiResponse(description="Deleted (soft)."), **WRITE_ERRORS}, tags=TAGS, description="DRAFT offers only."),
)
class OfferViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, DestroyModelMixin, BaseViewSet):
    module = "offers"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "create": "create",
        "partial_update": "edit",
        "destroy": "archive",
        "approve": "approve",
        "activate": "publish",
        "pause": "publish",
        "archive": "archive",
    }
    services = {"create": offers.create_offer, "update": offers.update_offer, "destroy": offers.delete_offer}
    http_method_names = ["get", "post", "patch", "delete"]
    lookup_value_regex = UUID_REGEX
    serializer_class = OfferSerializer
    filterset_class = OfferFilter
    search_fields = ["code", "name"]
    ordering_fields = ["created_at", "starts_on", "ends_on", "code"]
    ordering = ["-created_at"]

    def base_queryset(self):
        return offers.offers_queryset()

    def get_serializer_class(self):
        return {"create": OfferWriteSerializer, "partial_update": OfferUpdateSerializer}.get(self.action, OfferSerializer)

    def _run(self, service, request):
        body = ReasonSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        offer = service(self.get_object(), user=request.user, reason=body.validated_data.get("reason", ""), expected_version=body.validated_data.get("expected_version"))
        return Response(OfferSerializer(offers.offers_queryset().get(pk=offer.pk)).data)

    @_transition_schema("approve", "DRAFT → APPROVED.")
    @action(detail=True, methods=["post"])
    def approve(self, request, *args, **kwargs):
        return self._run(offers.approve, request)

    @_transition_schema("activate", "APPROVED → ACTIVE, or PAUSED → ACTIVE (resume). Refused once ends_on has passed.")
    @action(detail=True, methods=["post"])
    def activate(self, request, *args, **kwargs):
        return self._run(offers.activate, request)

    @_transition_schema("pause", "ACTIVE → PAUSED.")
    @action(detail=True, methods=["post"])
    def pause(self, request, *args, **kwargs):
        return self._run(offers.pause, request)

    @_transition_schema("archive", "Any status → ARCHIVED (terminal).")
    @action(detail=True, methods=["post"])
    def archive(self, request, *args, **kwargs):
        return self._run(offers.archive, request)
