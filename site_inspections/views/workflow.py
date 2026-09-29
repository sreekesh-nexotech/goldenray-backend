"""Workflow actions of ``site-inspections/<uid>/`` (POST sub-resources; ``status`` is never PATCHed)."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status as http_status
from rest_framework.decorators import action
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from site_inspections.serializers.inspections import (
    InspectionAssignSerializer,
    InspectionReasonSerializer,
    InspectionSerializer,
    InspectionVersionSerializer,
    ReportJobSerializer,
    SystemTypeSerializer,
)
from site_inspections.serializers.records import ApprovalRequestedSerializer, ApprovalRequestSerializer, ApprovalSerializer, PaperApprovalSerializer
from site_inspections.services import approvals, inspections, lifecycle, report

TAGS = ["site-inspections"]
ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}
WORKFLOW_PERMISSIONS = {
    "assign": "assign",
    "start": "edit",
    "submit": "submit",
    "hold": "approve",
    "resume": "approve",
    "revision": "approve",
    "release": "release",
    "system_type": "approve",
    "approval_request": "view",  # submit OR approve, checked by the service
    "approval_paper": "approve",
    "report": "view",
}


def _schema(operation_id: str, request, description: str, response=InspectionSerializer):
    return extend_schema(operation_id=operation_id, request=request, responses={200: response, **ERRORS}, tags=TAGS, description=description)


class WorkflowActionsMixin:
    def _run(self, serializer_class, fn, **extra):
        inspection = self.get_object()
        serializer = serializer_class(data=self.request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        updated = fn(inspection, user=self.request.user, expected_version=data.pop("expected_version", None), **data, **extra)
        return Response(InspectionSerializer(self.base_queryset().get(pk=updated.pk), context=self.get_serializer_context()).data)

    @_schema("site_inspections_assign", InspectionAssignSerializer, "Assign (or, with null, unassign) the field engineer. Needs `assign`.")
    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, *args, **kwargs):
        return self._run(InspectionAssignSerializer, inspections.assign)

    @_schema("site_inspections_start", InspectionVersionSerializer, "DRAFT / REVISION_REQUIRED → IN_PROGRESS (also implied by the first write). 409 `engineer_required`.")
    @action(detail=True, methods=["post"], url_path="start")
    def start(self, request, *args, **kwargs):
        return self._run(InspectionVersionSerializer, inspections.start)

    @_schema("site_inspections_submit", InspectionVersionSerializer, "IN_PROGRESS → COMPLETED when field completion is ready; 409 `completion_blocked` lists the blocker codes.")
    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, *args, **kwargs):
        return self._run(InspectionVersionSerializer, lifecycle.submit)

    @_schema("site_inspections_hold", InspectionReasonSerializer, "Put on hold with a reason (`approve`).")
    @action(detail=True, methods=["post"], url_path="hold")
    def hold(self, request, *args, **kwargs):
        return self._run(InspectionReasonSerializer, lifecycle.hold)

    @_schema("site_inspections_resume", InspectionVersionSerializer, "ON_HOLD → the status it was held from (`approve`).")
    @action(detail=True, methods=["post"], url_path="resume")
    def resume(self, request, *args, **kwargs):
        return self._run(InspectionVersionSerializer, lifecycle.resume)

    @_schema("site_inspections_revision", InspectionReasonSerializer, "Send back for revision (REVISION_REQUIRED; supersedes the standing approvals).")
    @action(detail=True, methods=["post"], url_path="revision")
    def revision(self, request, *args, **kwargs):
        return self._run(InspectionReasonSerializer, lifecycle.revision)

    @_schema("site_inspections_release", InspectionVersionSerializer, "APPROVED → INSTALLATION_READY when readiness has no blocker (409 `not_ready` lists them). Emits `site_inspections.released`.")
    @action(detail=True, methods=["post"], url_path="release")
    def release(self, request, *args, **kwargs):
        return self._run(InspectionVersionSerializer, lifecycle.release)

    @_schema("site_inspections_system_type", SystemTypeSerializer, "Project Head decision; after COMPLETED it needs a reason and sends the inspection back.")
    @action(detail=True, methods=["post"], url_path="system-type")
    def system_type(self, request, *args, **kwargs):
        return self._run(SystemTypeSerializer, inspections.set_system_type)

    @extend_schema(
        operation_id="site_inspections_approval_request",
        request=ApprovalRequestSerializer,
        responses={201: ApprovalRequestedSerializer, **ERRORS},
        tags=TAGS,
        description="Creates the PENDING location approval (snapshot of both rectangles) and returns the customer link once. Needs `submit` or `approve`.",
    )
    @action(detail=True, methods=["post"], url_path="approval/request")
    def approval_request(self, request, *args, **kwargs):
        inspection = self.get_object()
        serializer = ApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        approval, token = approvals.request_approval(
            inspection, user=request.user, customer_name=data.get("customer_name", ""), customer_phone_e164=data.get("customer_phone", ""), expected_version=data.get("expected_version")
        )
        return Response({"approval": ApprovalSerializer(approval).data, "link": approvals.link_for(token)}, status=http_status.HTTP_201_CREATED)

    @extend_schema(
        operation_id="site_inspections_approval_paper",
        request={"multipart/form-data": PaperApprovalSerializer},
        responses={200: ApprovalSerializer, **ERRORS},
        tags=TAGS,
        description="D-13 paper fallback: records the pending approval as APPROVED with the uploaded scan and a reason (`approve`, audited).",
    )
    @action(detail=True, methods=["post"], url_path="approval/paper")
    def approval_paper(self, request, *args, **kwargs):
        inspection = self.get_object()
        serializer = PaperApprovalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        approval = approvals.record_paper_approval(inspection, user=request.user, scan=data["scan"], reason=data["reason"], expected_version=data.get("expected_version"))
        return Response(ApprovalSerializer(approval).data)

    @extend_schema(
        operation_id="site_inspections_report",
        parameters=[OpenApiParameter("variant", str, enum=list(report.VARIANTS), default="customer")],
        responses={202: ReportJobSerializer, **ERRORS, 200: OpenApiResponse(ReportJobSerializer, description="An identical report was rendered before.")},
        tags=TAGS,
        description="Queues (or reuses) the PDF render; poll `documents/jobs/<job_uid>/` and ask it for a download URL.",
    )
    @action(detail=True, methods=["get"], url_path="report", filter_backends=[])
    def report(self, request, *args, **kwargs):
        job = report.request_report(self.get_object(), user=request.user, variant=request.query_params.get("variant", "customer"))
        return Response(ReportJobSerializer(job).data, status=http_status.HTTP_200_OK if job.status == "DONE" else http_status.HTTP_202_ACCEPTED)
