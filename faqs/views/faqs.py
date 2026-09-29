"""``faqs/`` — FAQ list, editor, workflow and reorder (module ``faqs``; PLAN §3.4 "Website content").

``faqs.view`` list/detail/``preview/`` · ``faqs.create`` POST · ``faqs.edit`` PATCH, ``reorder/`` ·
``faqs.publish`` ``publish/``, ``unpublish/`` · ``faqs.archive`` ``archive/``, ``restore/`` · ``faqs.verify``
``verify/``. There is no DELETE: archiving is how a FAQ leaves the site (its history stays readable).
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import BaseViewSet, CreateModelMixin, ListModelMixin, RetrieveModelMixin, UpdateModelMixin
from faqs.filters import FaqFilter
from faqs.models import Faq
from faqs.serializers import FaqActionSerializer, FaqCreateSerializer, FaqListSerializer, FaqPreviewSerializer, FaqReorderSerializer, FaqSerializer, FaqUpdateSerializer
from faqs.services import delivery, faqs
from flarize.filters import normalise_filter_params

UUID_REGEX = "[0-9a-fA-F-]{36}"
ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}


def _action_doc(description: str):
    return extend_schema(request=FaqActionSerializer, responses={200: FaqSerializer, **ERRORS}, description=description)


@extend_schema_view(
    list=extend_schema(
        responses={200: FaqListSerializer(many=True), 401: ErrorSerializer, 403: ErrorSerializer},
        parameters=[OpenApiParameter("include_archived", bool, description="Archived FAQs are hidden unless this is true or status=ARCHIVED.")],
    ),
    retrieve=extend_schema(responses={200: FaqSerializer, **READ_ERRORS}),
    create=extend_schema(request=FaqCreateSerializer, responses={201: FaqSerializer, **ERRORS}, description="Created as a DRAFT at the end of its page/section."),
    partial_update=extend_schema(request=FaqUpdateSerializer, responses={200: FaqSerializer, **ERRORS}, description="A published FAQ must stay complete."),
)
class FaqViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, UpdateModelMixin, BaseViewSet):
    module = "faqs"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "preview": "view",
        "create": "create",
        "partial_update": "edit",
        "reorder": "edit",
        "publish": "publish",
        "unpublish": "publish",
        "archive": "archive",
        "restore": "archive",
        "verify": "verify",
    }
    services = {"create": faqs.create_faq, "update": faqs.update_faq}
    http_method_names = ["get", "post", "patch"]
    lookup_value_regex = UUID_REGEX
    filterset_class = FaqFilter
    search_fields = ["question", "answer"]
    ordering_fields = ["question", "status", "sort_order", "created_at", "updated_at", "published_at"]

    def base_queryset(self):
        queryset = faqs.faqs_queryset()
        if self.action == "list":
            params = normalise_filter_params(self.request.query_params)
            if params.get("status") != Faq.Status.ARCHIVED and params.get("include_archived", "").lower() not in ("1", "true", "yes"):
                queryset = queryset.exclude(status=Faq.Status.ARCHIVED)
        return queryset

    def get_serializer_class(self):
        return {"list": FaqListSerializer, "create": FaqCreateSerializer, "partial_update": FaqUpdateSerializer}.get(self.action, FaqSerializer)

    def _workflow(self, request, service) -> Response:
        body = FaqActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        faq = service(self.get_object(), user=request.user, expected_version=body.validated_data.get("expected_version"))
        return Response(FaqSerializer(faqs.faqs_queryset().get(pk=faq.pk)).data)

    @_action_doc("DRAFT/ARCHIVED → PUBLISHED; 400 `faq_not_publishable` with the reasons while incomplete.")
    @action(detail=True, methods=["post"])
    def publish(self, request, *args, **kwargs):
        return self._workflow(request, faqs.publish)

    @_action_doc("PUBLISHED → DRAFT.")
    @action(detail=True, methods=["post"])
    def unpublish(self, request, *args, **kwargs):
        return self._workflow(request, faqs.unpublish)

    @_action_doc("DRAFT/PUBLISHED → ARCHIVED (off the site, history kept).")
    @action(detail=True, methods=["post"])
    def archive(self, request, *args, **kwargs):
        return self._workflow(request, faqs.archive)

    @_action_doc("ARCHIVED → DRAFT.")
    @action(detail=True, methods=["post"])
    def restore(self, request, *args, **kwargs):
        return self._workflow(request, faqs.restore)

    @_action_doc("Stamp the current content as reviewed; any later content edit clears it.")
    @action(detail=True, methods=["post"])
    def verify(self, request, *args, **kwargs):
        return self._workflow(request, faqs.verify)

    @extend_schema(request=None, responses={200: FaqPreviewSerializer, **READ_ERRORS}, description="The FAQ as the site will render it and the FAQPage schema of its section.")
    @action(detail=True, methods=["get"])
    def preview(self, request, *args, **kwargs):
        return Response(delivery.preview(self.get_object()))

    @extend_schema(request=FaqReorderSerializer, responses={200: FaqListSerializer(many=True), **ERRORS}, description="Renumber one page/section atomically; unknown uids are ignored.")
    @action(detail=False, methods=["post"], pagination_class=None, filter_backends=[])  # a plain array, not a filtered list
    def reorder(self, request, *args, **kwargs):
        body = FaqReorderSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        rows = faqs.reorder(data["page"], data.get("section", ""), data["order"], user=request.user)
        refreshed = {faq.pk: faq for faq in faqs.faqs_queryset().filter(pk__in=[row.pk for row in rows])}
        return Response(FaqListSerializer([refreshed[row.pk] for row in rows], many=True).data)
