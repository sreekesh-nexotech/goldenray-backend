"""``hr/holidays/``, ``hr/leave-types/``, ``hr/attendance-rules/`` (module ``hr_setup``) and ``hr/leave/`` (module
``leave``: view / create / approve; record scope all / office / self).

Leave: list/detail (scoped), ``POST`` (self-service → PENDING; an approver filing for someone else → APPROVED),
``approve/`` and ``reject/`` (``leave.approve``, never on your own leave), ``cancel/`` (``leave.view`` gate; the service
requires ``leave.create`` for your own pending or not-yet-started leave and ``leave.approve`` for anyone else's). There
is no PATCH or DELETE: cancel and file again. Leave types are read with ``leave.view`` (the vocabulary of self-service)
and written with ``hr_setup.edit``.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.views import BaseViewSet, CreateModelMixin, ListModelMixin, RetrieveModelMixin
from hr.filters import AttendanceRuleFilter, HolidayFilter, LeaveFilter
from hr.serializers.calendar import (
    AttendanceRuleCreateSerializer,
    AttendanceRuleSerializer,
    AttendanceRuleUpdateSerializer,
    HolidayCreateSerializer,
    HolidaySerializer,
    HolidayUpdateSerializer,
    LeaveActionSerializer,
    LeaveCreateSerializer,
    LeaveRecordSerializer,
    LeaveTypeCreateSerializer,
    LeaveTypeSerializer,
    LeaveTypeUpdateSerializer,
)
from hr.services import holidays, leave, rules
from hr.views.setup import READ_ERRORS, SETUP_PERMISSIONS, UUID_REGEX, WRITE_ERRORS, HrCrudViewSet, crud_schema

TAGS = ["hr"]


@crud_schema("hr_holidays", HolidaySerializer, HolidayCreateSerializer, HolidayUpdateSerializer, "Soft delete. 409 `holiday_exists` guards one holiday per office and date (and one global per date).")
class HolidayViewSet(HrCrudViewSet):
    module = "hr_setup"
    action_permissions = SETUP_PERMISSIONS
    services = {"create": holidays.create_holiday, "update": holidays.update_holiday, "destroy": holidays.delete_holiday}
    serializer_class = HolidaySerializer
    write_serializers = {"create": HolidayCreateSerializer, "partial_update": HolidayUpdateSerializer}
    filterset_class = HolidayFilter
    search_fields = ["name"]
    ordering_fields = ["date", "name", "created_at"]
    ordering = ["date"]

    def base_queryset(self):
        return holidays.holidays_queryset()


@crud_schema("hr_leave_types", LeaveTypeSerializer, LeaveTypeCreateSerializer, LeaveTypeUpdateSerializer, "Soft delete; 409 `leave_type_in_use` while leave records use it.")
class LeaveTypeViewSet(HrCrudViewSet):
    module = "hr_setup"
    # Read by whoever files or decides leave (Staff, Office Manager, HR: leave.view); written by HR setup.
    action_permissions = {**SETUP_PERMISSIONS, "list": ("leave", "view"), "retrieve": ("leave", "view")}
    services = {"create": leave.create_leave_type, "update": leave.update_leave_type, "destroy": leave.delete_leave_type}
    serializer_class = LeaveTypeSerializer
    write_serializers = {"create": LeaveTypeCreateSerializer, "partial_update": LeaveTypeUpdateSerializer}
    search_fields = ["code", "name"]
    ordering_fields = ["code", "name"]
    ordering = ["name"]

    def base_queryset(self):
        return leave.leave_types_queryset()

    def scope_queryset(self, queryset, module=None):
        # A lookup without record scope (hr_setup is "all"-only; the leave scopes filter leave *records*): a Staff reader
        # holds no hr_setup scope, which would otherwise empty the list. The permission check above still applies.
        return queryset


@crud_schema("hr_attendance_rules", AttendanceRuleSerializer, AttendanceRuleCreateSerializer, AttendanceRuleUpdateSerializer, "Soft delete; re-computes the people the rule covered.")
class AttendanceRuleViewSet(HrCrudViewSet):
    module = "hr_setup"
    action_permissions = SETUP_PERMISSIONS
    services = {"create": rules.create_rule, "update": rules.update_rule, "destroy": rules.delete_rule}
    serializer_class = AttendanceRuleSerializer
    write_serializers = {"create": AttendanceRuleCreateSerializer, "partial_update": AttendanceRuleUpdateSerializer}
    filterset_class = AttendanceRuleFilter
    search_fields = ["name"]
    ordering_fields = ["name", "effective_from", "created_at"]
    ordering = ["name"]

    def base_queryset(self):
        return rules.rules_queryset()


def _decision_schema(name: str, description: str):
    return extend_schema(operation_id=f"hr_leave_{name}", request=LeaveActionSerializer, responses={200: LeaveRecordSerializer, **WRITE_ERRORS}, tags=TAGS, description=description)


@extend_schema_view(
    list=extend_schema(operation_id="hr_leave_list", tags=TAGS),
    retrieve=extend_schema(operation_id="hr_leave_retrieve", responses={200: LeaveRecordSerializer, **READ_ERRORS}, tags=TAGS),
    create=extend_schema(
        operation_id="hr_leave_create",
        request=LeaveCreateSerializer,
        responses={201: LeaveRecordSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Self-service (no `employee`, or your own) → PENDING unless the type needs no approval; an approver filing for someone else → APPROVED. 409 `leave_overlap`.",
    ),
)
class LeaveViewSet(ListModelMixin, RetrieveModelMixin, CreateModelMixin, BaseViewSet):
    module = "leave"
    action_permissions = {"list": "view", "retrieve": "view", "create": "create", "cancel": "view", "approve": "approve", "reject": "approve"}
    services = {"create": leave.create_leave}
    http_method_names = ["get", "post"]
    lookup_value_regex = UUID_REGEX
    serializer_class = LeaveRecordSerializer
    filterset_class = LeaveFilter
    search_fields = ["employee__code", "employee__full_name", "reason"]
    ordering_fields = ["date_from", "date_to", "created_at", "status"]
    ordering = ["-date_from"]

    def base_queryset(self):
        return leave.leave_queryset()

    def get_serializer_class(self):
        return LeaveCreateSerializer if self.action == "create" else LeaveRecordSerializer

    def _act(self, request, service):
        record = self.get_object()
        body = LeaveActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        updated = service(record, user=request.user, expected_version=body.validated_data.get("expected_version"), note=body.validated_data["note"])
        return Response(LeaveRecordSerializer(leave.leave_queryset().get(pk=updated.pk)).data)

    @_decision_schema("approve", "PENDING → APPROVED. 409 `leave_not_pending`, `leave_overlap`; 403 `self_action_denied` on your own leave.")
    @action(detail=True, methods=["post"])
    def approve(self, request, *args, **kwargs):
        return self._act(request, leave.approve_leave)

    @_decision_schema("reject", "PENDING → REJECTED (put the reason in `note`). 409 `leave_not_pending`; 403 `self_action_denied` on your own leave.")
    @action(detail=True, methods=["post"])
    def reject(self, request, *args, **kwargs):
        return self._act(request, leave.reject_leave)

    @_decision_schema(
        "cancel",
        "PENDING/APPROVED → CANCELLED: your own pending or not-yet-started leave (`leave.create`), or anyone's as an approver (`leave.approve`); "
        "else 403. 409 `leave_not_cancellable`, `leave_already_started`.",
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, *args, **kwargs):
        return self._act(request, leave.cancel_leave)
