"""Reviews, additional work, approvals, readiness, activity and snapshots of ``site-inspections/<uid>/``."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.response import Response

from audit.services.queries import history
from core.serializers import ErrorSerializer
from flarize.pagination import CreatedAtCursorPagination, StandardPagination
from site_inspections.models import EngineeringReview, Snapshot
from site_inspections.serializers.common import ReadinessSerializer
from site_inspections.serializers.inspections import ReviewDecisionSerializer, ReviewRequestSerializer, SnapshotSerializer
from site_inspections.serializers.records import (
    ActivitySerializer,
    ApprovalSerializer,
    EngineeringReviewSerializer,
    WorkItemCreateSerializer,
    WorkItemSerializer,
    WorkItemWriteSerializer,
    WorkTransitionSerializer,
)
from site_inspections.services import approvals, common, reviews, work

TAGS = ["site-inspections"]
ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
READ = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
UID = "[0-9a-fA-F-]{36}"
DECISION_PERMISSIONS = {
    "reviews": "view",
    "request_review": "edit",
    "decide_review": "approve",
    "additional_work": "view",
    "add_work": "edit",
    "work_item": "edit",
    "delete_work": "edit",
    "transition_work": "view",  # edit to re-identify, approve for every decision — checked by the service
    "approvals": "view",
    "readiness": "view",
    "activity": "view",
    "snapshots": "view",
}


class ActivityPagination(CreatedAtCursorPagination):
    ordering = ("-at", "-id")


class DecisionActionsMixin:
    # ── engineering reviews ─────────────────────────────────────────────────────────────────────────────────────────
    @extend_schema(operation_id="site_inspections_reviews_list", responses={200: EngineeringReviewSerializer(many=True), **READ}, tags=TAGS)
    @action(detail=True, methods=["get"], url_path="reviews", filter_backends=[], pagination_class=StandardPagination)
    def reviews(self, request, *args, **kwargs):
        queryset = EngineeringReview.objects.filter(inspection=self.get_object()).select_related("requested_by", "reviewer").order_by("-created_at", "-id")
        return self._page(queryset, EngineeringReviewSerializer)

    @extend_schema(
        operation_id="site_inspections_reviews_request",
        request=ReviewRequestSerializer,
        responses={201: EngineeringReviewSerializer, **ERRORS},
        tags=TAGS,
        description="Manual request; 409 `review_pending`.",
    )
    @reviews.mapping.post
    def request_review(self, request, *args, **kwargs):
        serializer = ReviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        review = reviews.request_review(self.get_object(), user=request.user, reason=serializer.validated_data["reason"], expected_version=serializer.validated_data.get("expected_version"))
        return Response(EngineeringReviewSerializer(review).data, status=http_status.HTTP_201_CREATED)

    @extend_schema(operation_id="site_inspections_reviews_decide", request=ReviewDecisionSerializer, responses={200: EngineeringReviewSerializer, **ERRORS}, tags=TAGS)
    @action(detail=True, methods=["post"], url_path=f"reviews/(?P<review_uid>{UID})/decide")
    def decide_review(self, request, *args, review_uid=None, **kwargs):
        review = reviews.get_review(self.get_object(), review_uid)
        serializer = ReviewDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        review = reviews.decide_review(review, user=request.user, decision=data["decision"], notes=data.get("notes", ""), expected_version=data.get("expected_version"))
        return Response(EngineeringReviewSerializer(review).data)

    # ── additional work ─────────────────────────────────────────────────────────────────────────────────────────────
    @extend_schema(operation_id="site_inspections_work_list", responses={200: WorkItemSerializer(many=True), **READ}, tags=TAGS, description="At most one item per work type.")
    @action(detail=True, methods=["get"], url_path="additional-work", filter_backends=[], pagination_class=None)
    def additional_work(self, request, *args, **kwargs):
        return Response(WorkItemSerializer(work.items_queryset(self.get_object()), many=True).data)

    @extend_schema(
        operation_id="site_inspections_work_create",
        request=WorkItemCreateSerializer,
        responses={201: WorkItemSerializer, **ERRORS},
        tags=TAGS,
        description="Identify an item (IDENTIFIED); 409 `work_type_exists`.",
    )
    @additional_work.mapping.post
    def add_work(self, request, *args, **kwargs):
        serializer = WorkItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = work.create_item(self.get_object(), user=request.user, data=serializer.validated_data)
        return Response(WorkItemSerializer(item).data, status=http_status.HTTP_201_CREATED)

    @extend_schema(operation_id="site_inspections_work_update", request=WorkItemWriteSerializer, responses={200: WorkItemSerializer, **ERRORS}, tags=TAGS, description="IDENTIFIED items only.")
    @action(detail=True, methods=["patch"], url_path=f"additional-work/(?P<item_uid>{UID})")
    def work_item(self, request, *args, item_uid=None, **kwargs):
        item = work.get_item(self.get_object(), item_uid)
        serializer = WorkItemWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        item = work.update_item(item, user=request.user, data=data, expected_version=data.pop("expected_version", None))
        return Response(WorkItemSerializer(item).data)

    @extend_schema(operation_id="site_inspections_work_delete", responses={204: OpenApiResponse(description="Removed (soft)."), **ERRORS}, tags=TAGS)
    @work_item.mapping.delete
    def delete_work(self, request, *args, item_uid=None, **kwargs):
        work.delete_item(work.get_item(self.get_object(), item_uid), user=request.user, expected_version=self.expected_version())
        return Response(status=http_status.HTTP_204_NO_CONTENT)

    @extend_schema(
        operation_id="site_inspections_work_transition",
        request=WorkTransitionSerializer,
        responses={200: WorkItemSerializer, **ERRORS},
        tags=TAGS,
        description="The enforced lifecycle; `approve` for every move but NONE → IDENTIFIED (`edit`); COST_CALCULATED needs `agreement_uid`.",
    )
    @action(detail=True, methods=["post"], url_path=f"additional-work/(?P<item_uid>{UID})/transition")
    def transition_work(self, request, *args, item_uid=None, **kwargs):
        item = work.get_item(self.get_object(), item_uid)
        serializer = WorkTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        item = work.transition_item(item, user=request.user, status=data["status"], agreement_uid=data.get("agreement_uid"), note=data.get("note", ""), expected_version=data.get("expected_version"))
        return Response(WorkItemSerializer(item).data)

    # ── approvals, readiness, activity, snapshots ───────────────────────────────────────────────────────────────────
    @extend_schema(operation_id="site_inspections_approvals_list", responses={200: ApprovalSerializer(many=True), **READ}, tags=TAGS)
    @action(detail=True, methods=["get"], url_path="approvals", filter_backends=[], pagination_class=StandardPagination)
    def approvals(self, request, *args, **kwargs):
        return self._page(approvals.approvals_queryset(self.get_object()), ApprovalSerializer)

    @extend_schema(
        operation_id="site_inspections_readiness", responses={200: ReadinessSerializer, **READ}, tags=TAGS, description="Blocker codes + text, checks and field completion, on the current row."
    )
    @action(detail=True, methods=["get"], url_path="readiness", filter_backends=[], pagination_class=None)
    def readiness(self, request, *args, **kwargs):
        return Response(common.readiness(self.get_object()))

    @extend_schema(operation_id="site_inspections_activity", responses={200: ActivitySerializer(many=True), **READ}, tags=TAGS, description="The inspection's audit trail, newest first (cursor).")
    @action(detail=True, methods=["get"], url_path="activity", filter_backends=[], pagination_class=ActivityPagination)
    def activity(self, request, *args, **kwargs):
        paginator = ActivityPagination()
        page = paginator.paginate_queryset(history(common.OBJECT_TYPE, self.get_object().uid), request, view=self)
        return paginator.get_paginated_response(ActivitySerializer(page, many=True).data)

    @extend_schema(operation_id="site_inspections_snapshots", responses={200: SnapshotSerializer(many=True), **READ}, tags=TAGS, description="Snapshot versions, newest first.")
    @action(detail=True, methods=["get"], url_path="snapshots", filter_backends=[], pagination_class=StandardPagination)
    def snapshots(self, request, *args, **kwargs):
        return self._page(Snapshot.objects.filter(inspection=self.get_object()).order_by("-number"), SnapshotSerializer)
