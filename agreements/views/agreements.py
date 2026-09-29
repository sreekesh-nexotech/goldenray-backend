"""``agreements/`` — list/detail (record scope: Sales Executives see what they own), blank create, from-quotation, DRAFT
edit, issue, supersede, cancel, paper acceptance, document link and render. The price override is in
:mod:`agreements.views.price_override` (flag-gated).

``agreements.view`` reads · ``create`` creates · ``edit`` edits drafts, cancels drafts, records acceptance, renders ·
``issue`` issues and supersedes · ``manage`` additionally cancels issued agreements and overrides prices.
"""

from __future__ import annotations

import django_filters
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from agreements.models import Agreement, AgreementKind, AgreementStatus, Language
from agreements.serializers.agreements import (
    AcceptanceSerializer,
    AgreementCancelSerializer,
    AgreementDetailSerializer,
    AgreementDocumentLinkSerializer,
    AgreementRenderJobSerializer,
    AgreementRenderSerializer,
    AgreementSerializer,
    AgreementTransitionSerializer,
    AgreementUpdateSerializer,
    BlankAgreementSerializer,
    FromQuotationSerializer,
    SupersedeSerializer,
)
from agreements.services import acceptance, agreements, issuing
from agreements.services.common import agreements_queryset, reload
from core.serializers import ErrorSerializer
from core.views import BaseViewSet, ListModelMixin, RetrieveModelMixin

TAGS = ["agreements"]
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer, 422: ErrorSerializer}
LANGUAGE = OpenApiParameter("language", str, enum=Language.values, description="Document language (default: the agreement's).")


class AgreementFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=AgreementStatus.choices)
    kind = django_filters.ChoiceFilter(choices=AgreementKind.choices)
    customer = django_filters.UUIDFilter(field_name="customer__uid")
    owner = django_filters.UUIDFilter(field_name="owner__uid")
    quotation = django_filters.UUIDFilter(field_name="quotation_version__quotation__uid")
    source_uid = django_filters.UUIDFilter()
    legacy = django_filters.BooleanFilter()

    class Meta:
        model = Agreement
        fields: list[str] = []


@extend_schema_view(
    list=extend_schema(operation_id="agreements_list", tags=TAGS),
    retrieve=extend_schema(operation_id="agreements_retrieve", responses={200: AgreementDetailSerializer, **READ_ERRORS}, tags=TAGS),
)
class AgreementViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "agreements"
    action_permissions = {
        "list": "view",
        "retrieve": "view",
        "create": "create",
        "from_quotation": "create",
        "partial_update": "edit",
        "issue": "issue",
        "supersede": "issue",
        "cancel": "edit",
        "record_acceptance": "edit",
        "document": "view",
        "render": "edit",
    }
    http_method_names = ["get", "post", "patch"]
    serializer_class = AgreementSerializer
    filterset_class = AgreementFilter
    search_fields = ["number", "customer__name", "customer__code", "legacy_quotation_ref"]
    ordering_fields = ["created_at", "number", "issued_at", "status"]
    ordering = ["-created_at"]

    def base_queryset(self):
        queryset = agreements_queryset()
        if self.action == "retrieve":
            queryset = queryset.prefetch_related("lines").select_related("panel", "inverter", "battery", "structure_template")
        return queryset

    def get_serializer_class(self):
        return AgreementDetailSerializer if self.action == "retrieve" else AgreementSerializer

    def _detail(self, agreement, status=200):
        agreement = reload(agreement)
        return Response(AgreementDetailSerializer(agreement, context=self.get_serializer_context()).data, status=status)

    @extend_schema(
        operation_id="agreements_create",
        request=BlankAgreementSerializer,
        responses={201: AgreementDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="A blank SALE_ORDER or EXTRA_STRUCTURE DRAFT (no quotation); Extra Structure is priced by its lines and names the site inspection (source_type/source_uid).",
    )
    def create(self, request, *args, **kwargs):
        body = BlankAgreementSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(agreements.create_blank(user=request.user, data=body.validated_data), status=201)

    @extend_schema(
        operation_id="agreements_from_quotation",
        request=FromQuotationSerializer,
        responses={201: AgreementDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="A PURCHASE_AGREEMENT or SALE_ORDER DRAFT pinned to an ISSUED quotation version: system, equipment, prices and the KSEB fee are copied, never typed.",
    )
    @action(detail=False, methods=["post"], url_path="from-quotation", filter_backends=[], pagination_class=None)
    def from_quotation(self, request, *args, **kwargs):
        body = FromQuotationSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(agreements.create_from_quotation(user=request.user, data=body.validated_data), status=201)

    @extend_schema(
        operation_id="agreements_partial_update",
        request=AgreementUpdateSerializer,
        responses={200: AgreementDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Edit a DRAFT. A quotation-derived draft keeps its pinned system, equipment and prices (400 `field_pinned`).",
    )
    def partial_update(self, request, *args, **kwargs):
        agreement = self.get_object()
        body = AgreementUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        expected = data.pop("expected_version", None)
        return self._detail(agreements.update_draft(agreement, user=request.user, data=data, expected_version=expected))

    @extend_schema(
        operation_id="agreements_issue",
        request=AgreementTransitionSerializer,
        responses={200: AgreementDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Issue the DRAFT: its form's fields present (422 `agreement_incomplete`), number AGR-<FY>-<nnnn>, frozen document + SHA-256, rendered; agreements.issued (+ .superseded).",
    )
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def issue(self, request, *args, **kwargs):
        body = AgreementTransitionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._detail(issuing.issue(self.get_object(), user=request.user, expected_version=body.validated_data.get("expected_version")))

    @extend_schema(
        operation_id="agreements_supersede",
        request=SupersedeSerializer,
        responses={201: AgreementDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="A new DRAFT revision of an ISSUED or ACCEPTED agreement; the old one is SUPERSEDED when the revision is issued.",
    )
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def supersede(self, request, *args, **kwargs):
        body = SupersedeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = dict(body.validated_data)
        expected = data.pop("expected_version", None)
        return self._detail(agreements.supersede(self.get_object(), user=request.user, data=data, expected_version=expected), status=201)

    @extend_schema(
        operation_id="agreements_cancel",
        request=AgreementCancelSerializer,
        responses={200: AgreementDetailSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Cancel a DRAFT, or (with agreements.manage) an ISSUED/ACCEPTED agreement; the reason is kept.",
    )
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def cancel(self, request, *args, **kwargs):
        body = AgreementCancelSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        agreement = agreements.cancel(self.get_object(), user=request.user, reason=body.validated_data["reason"], expected_version=body.validated_data.get("expected_version"))
        return self._detail(agreement)

    @extend_schema(
        operation_id="agreements_record_acceptance",
        request={"multipart/form-data": AcceptanceSerializer},
        responses={200: AgreementDetailSerializer, **WRITE_ERRORS, 413: ErrorSerializer},
        tags=TAGS,
        description="The customer signed the paper copy: the ISSUED agreement becomes ACCEPTED (via PAPER) and the scan is kept as private media.",
    )
    @action(detail=True, methods=["post"], url_path="record-acceptance", parser_classes=[MultiPartParser, FormParser], filter_backends=[], pagination_class=None)
    def record_acceptance(self, request, *args, **kwargs):
        body = AcceptanceSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        agreement = acceptance.record_acceptance(self.get_object(), user=request.user, file=data["file"], note=data.get("note", ""), expected_version=data.get("expected_version"))
        return self._detail(agreement)

    @extend_schema(
        operation_id="agreements_document",
        parameters=[LANGUAGE],
        responses={200: AgreementDocumentLinkSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="A single-use signed download link of the rendered PDF (409 `document_not_ready` until rendered).",
    )
    @action(detail=True, methods=["get"], filter_backends=[], pagination_class=None)
    def document(self, request, *args, **kwargs):
        from documents.services.downloads import signed_url

        agreement = self.get_object()
        language = request.query_params.get("language")
        language = language if language in Language.values else agreement.language
        link = signed_url(issuing.document_job(agreement, language), user=request.user, version=request.version, absolute=request.build_absolute_uri)
        return Response(AgreementDocumentLinkSerializer({"url": link.url, "expires_at": link.expires_at, "language": language}).data)

    @extend_schema(
        operation_id="agreements_render",
        parameters=[LANGUAGE],
        request=AgreementRenderSerializer,
        responses={200: AgreementRenderJobSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Render the document in a language (`language` in the body or the query): an issued agreement's frozen payload, a DRAFT's current document. The payload is never changed.",
    )
    @action(detail=True, methods=["post"], filter_backends=[], pagination_class=None)
    def render(self, request, *args, **kwargs):
        data = request.data if request.data else {"language": request.query_params.get("language")}
        body = AgreementRenderSerializer(data=data)
        body.is_valid(raise_exception=True)
        job = issuing.render(self.get_object(), user=request.user, language=body.validated_data["language"])
        return Response(AgreementRenderJobSerializer({"uid": job.uid, "status": job.status, "language": job.language, "payload_sha256": job.payload_sha256}).data)
