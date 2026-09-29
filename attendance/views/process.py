"""``attendance/process/``, ``attendance/process-all/``, ``attendance/recalculate/`` (``attendance.manage``) and
``attendance/corrections/`` (list: ``view`` within scope; create / revoke: ``edit``, never on your own day)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from attendance.filters import AttendanceCorrectionFilter
from attendance.models import AttendanceDay
from attendance.serializers.process import (
    AttendanceCorrectionSerializer,
    CorrectionCreateSerializer,
    CorrectionRevokeSerializer,
    ProcessAllResultSerializer,
    ProcessRequestSerializer,
    RecalculateRequestSerializer,
    RecalculateResultSerializer,
    RecomputeResultSerializer,
)
from attendance.services import corrections, days, processing, scopes
from core.errors import NotFound
from core.serializers import ErrorSerializer
from core.views import BaseAPIView, BaseViewSet, ListModelMixin, RetrieveModelMixin

from .days import READ_ERRORS, TAGS, UUID_REGEX

WRITE_ERRORS = {**READ_ERRORS, 409: ErrorSerializer}


class ProcessView(BaseAPIView):
    module = "attendance"
    action_permissions = {"POST": "manage"}

    @extend_schema(
        operation_id="attendance_process",
        request=ProcessRequestSerializer,
        responses={200: RecomputeResultSerializer, **READ_ERRORS},
        tags=TAGS,
        description=(
            f"Recompute a window (at most {processing.MAX_PROCESS_DAYS} days; 400 `range_too_long`). Raw punches are untouched, corrected days kept, nothing stored for today or later. "
            "Only employees within the caller's attendance record scope: a named employee outside it answers 404 `not_found` (B-8)."
        ),
    )
    def post(self, request, *args, **kwargs):
        body = ProcessRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(RecomputeResultSerializer(processing.process(user=request.user, **body.validated_data)).data)


class ProcessAllView(BaseAPIView):
    module = "attendance"
    action_permissions = {"POST": "manage"}

    @extend_schema(
        operation_id="attendance_process_all",
        request=None,
        responses={202: ProcessAllResultSerializer, **READ_ERRORS},
        tags=TAGS,
        description="Queue the recompute of every date that has punches (runs in the background worker).",
    )
    def post(self, request, *args, **kwargs):
        return Response(ProcessAllResultSerializer(processing.process_all(user=request.user)).data, status=status.HTTP_202_ACCEPTED)


class RecalculateView(BaseAPIView):
    module = "attendance"
    action_permissions = {"POST": "manage"}

    @extend_schema(
        operation_id="attendance_recalculate",
        request=RecalculateRequestSerializer,
        responses={200: RecalculateResultSerializer, **READ_ERRORS},
        tags=TAGS,
        description=(
            "Recompute a window (default: yesterday) and report, per terminal, how its punches arrive. The server never dials a terminal. "
            "Only employees within the caller's attendance record scope: a named employee outside it answers 404 `not_found` (B-8)."
        ),
    )
    def post(self, request, *args, **kwargs):
        body = RecalculateRequestSerializer(data=request.data or {})
        body.is_valid(raise_exception=True)
        return Response(RecalculateResultSerializer(processing.recalculate(user=request.user, **body.validated_data)).data)


@extend_schema_view(
    list=extend_schema(operation_id="attendance_corrections_list", tags=TAGS),
    retrieve=extend_schema(operation_id="attendance_corrections_retrieve", responses={200: AttendanceCorrectionSerializer, **READ_ERRORS}, tags=TAGS),
)
class CorrectionViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "attendance"
    action_permissions = {"list": "view", "retrieve": "view", "create": "edit", "revoke": "edit"}
    http_method_names = ["get", "post"]
    lookup_value_regex = UUID_REGEX
    serializer_class = AttendanceCorrectionSerializer
    filterset_class = AttendanceCorrectionFilter
    search_fields = ["day__employee__full_name", "day__employee__code", "reason"]
    ordering_fields = ["created_at", "revoked_at"]
    ordering = ["-created_at"]

    def base_queryset(self):
        return days.corrections_queryset()

    @extend_schema(
        operation_id="attendance_corrections_create",
        request=CorrectionCreateSerializer,
        responses={201: AttendanceCorrectionSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description=(
            "Correct one field of a stored day (the day is then left alone by the recompute until revoked). "
            "403 `self_action_denied` on your own day; 409 `correction_exists`, `stale_version` (the day's version)."
        ),
    )
    def create(self, request, *args, **kwargs):
        body = CorrectionCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        day = scopes.scoped(AttendanceDay.objects.all(), request.user).filter(uid=data["day_uid"]).first()
        if day is None:
            raise NotFound("not_found", "Attendance day not found.")
        correction = corrections.create_correction(user=request.user, day=day, field=data["field"], new=data["new"], reason=data["reason"], expected_version=data.get("expected_version"))
        return Response(AttendanceCorrectionSerializer(days.corrections_queryset().get(pk=correction.pk)).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        operation_id="attendance_corrections_revoke",
        request=CorrectionRevokeSerializer,
        responses={200: AttendanceCorrectionSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Revoke: the field gets its computed value back; with no active correction left the day is recomputed. 409 `correction_revoked`, `correction_revoke_conflict`, `stale_version`.",
    )
    @action(detail=True, methods=["post"])
    def revoke(self, request, *args, **kwargs):
        correction = self.get_object()
        body = CorrectionRevokeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        corrections.revoke_correction(user=request.user, correction=correction, reason=body.validated_data["reason"], expected_version=body.validated_data.get("expected_version"))
        return Response(AttendanceCorrectionSerializer(days.corrections_queryset().get(pk=correction.pk)).data)
