"""``attendance/days/`` (processed days), ``attendance/raw/`` (raw punches, cursor) and the day timeline
(``attendance/employees/<uid>/timeline/``) — module ``attendance`` (view), record scope all / office / self."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.response import Response

from attendance.filters import AttendanceDayFilter
from attendance.serializers.days import AttendanceDaySerializer, AttendanceRawPunchSerializer, AttendanceTimelineSerializer, RawQuerySerializer, TimelineQuerySerializer
from attendance.services import days, inputs, scopes
from core.serializers import ErrorSerializer
from core.views import BaseAPIView, BaseViewSet, ListModelMixin, RetrieveModelMixin
from flarize.pagination import CreatedAtCursorPagination

TAGS = ["attendance"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
READ_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}


@extend_schema_view(
    list=extend_schema(operation_id="attendance_days_list", tags=TAGS, description="Stored (final) days. Today and later are never stored (A7): use `attendance/day/` for today."),
    retrieve=extend_schema(operation_id="attendance_days_retrieve", responses={200: AttendanceDaySerializer, **READ_ERRORS}, tags=TAGS),
)
class AttendanceDayViewSet(ListModelMixin, RetrieveModelMixin, BaseViewSet):
    module = "attendance"
    action_permissions = {"list": "view", "retrieve": "view"}
    http_method_names = ["get"]
    lookup_value_regex = UUID_REGEX
    serializer_class = AttendanceDaySerializer
    filterset_class = AttendanceDayFilter
    search_fields = ["employee__full_name", "employee__code"]
    ordering_fields = ["work_date", "employee__full_name", "working_minutes", "late_minutes", "status"]
    ordering = ["-work_date", "employee__full_name"]

    def base_queryset(self):
        queryset = days.days_queryset()
        include_inactive = str(self.request.query_params.get("include_inactive", "")).lower() in ("1", "true", "yes")
        return queryset if include_inactive else queryset.filter(employee__is_active=True)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["origins"] = getattr(self, "_origins", {})
        return context

    def paginate_queryset(self, queryset):
        page = super().paginate_queryset(queryset)
        self._origins = days.origins(page or [])
        return page

    def get_object(self):
        instance = super().get_object()
        self._origins = days.origins([instance])
        return instance


class RawPunchPagination(CreatedAtCursorPagination):
    ordering = ("-punch_at", "-id")
    page_size = 100
    max_page_size = 1000


@extend_schema_view(
    list=extend_schema(
        operation_id="attendance_raw_list",
        parameters=[RawQuerySerializer],
        tags=TAGS,
        description="Punches exactly as the terminals delivered them, newest first (cursor). Read-only: nothing here is ever edited. A punch is identified by its `dedup_key`.",
    )
)
class RawPunchViewSet(ListModelMixin, BaseViewSet):
    module = "attendance"
    action_permissions = {"list": "view"}
    http_method_names = ["get"]
    serializer_class = AttendanceRawPunchSerializer
    pagination_class = RawPunchPagination
    filter_backends: list = []

    def base_queryset(self):
        return days.raw_queryset()

    def filter_queryset(self, queryset):
        query = RawQuerySerializer(data=self.request.query_params)
        query.is_valid(raise_exception=True)
        return days.filter_raw(queryset, **query.validated_data)

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        serializer = self.get_serializer(page, many=True, context={**self.get_serializer_context(), "people": days.people_by_pin(page)})
        return self.get_paginated_response(serializer.data)


class TimelineView(BaseAPIView):
    module = "attendance"
    action_permissions = {"GET": "view"}

    @extend_schema(
        operation_id="attendance_employee_timeline",
        parameters=[TimelineQuerySerializer],
        responses={200: AttendanceTimelineSerializer, **READ_ERRORS},
        tags=TAGS,
        description="The processed day plus every punch of the person's linked PINs from the day before to the day after, each marked accepted / ignored (double scan) / other day.",
    )
    def get(self, request, uid, *args, **kwargs):
        query = TimelineQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        employee = days.employee_or_404(scopes.employees_in_scope(request.user, inputs.employees_queryset()), uid)
        data = days.timeline(employee, query.validated_data["work_date"])
        day = data["day"]
        context = {"request": request, "origins": {day.pk: data["origin"]} if day is not None else {}, "people": {(row["punch"].device_id, row["punch"].pin): employee for row in data["punches"]}}
        return Response(AttendanceTimelineSerializer(data, context=context).data)
