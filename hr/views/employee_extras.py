"""``hr/employees/`` extras: private photo, device mappings, dependencies, device reconciliation (a mixin of
``EmployeeViewSet``; the devices package fills the device parts through ``hr.registries``)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.services import parse_expected_version
from hr.serializers.employees import (
    DependenciesSerializer,
    DeviceMappingsSerializer,
    EmployeeSerializer,
    PhotoUploadSerializer,
    ReconcileRequestSerializer,
    ReconcileResultSerializer,
)
from hr.services import employee_links, employees

TAGS = ["hr"]
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, **READ_ERRORS, 409: ErrorSerializer}


class EmployeeExtrasMixin:
    def _employee_response(self, employee):
        return Response(EmployeeSerializer(employees.employees_queryset().get(pk=employee.pk), context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="hr_employees_photo_set",
        request={"multipart/form-data": PhotoUploadSerializer},
        responses={200: EmployeeSerializer, 413: ErrorSerializer, 503: ErrorSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Upload or replace the photo (PRIVATE PHOTO media, hidden from the media library); the previous file is deleted.",
    )
    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def photo(self, request, *args, **kwargs):
        employee = self.get_object()
        body = PhotoUploadSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        updated = employee_links.set_photo(employee, user=request.user, file=body.validated_data["file"], expected_version=body.validated_data.get("expected_version"))
        return self._employee_response(updated)

    @extend_schema(
        operation_id="hr_employees_photo_remove",
        responses={200: EmployeeSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Remove the photo (`?expected_version=`); 404 `no_photo` when there is none.",
    )
    @photo.mapping.delete
    def remove_photo(self, request, *args, **kwargs):
        employee = self.get_object()
        updated = employee_links.remove_photo(employee, user=request.user, expected_version=parse_expected_version(request.query_params.get("expected_version")))
        return self._employee_response(updated)

    @extend_schema(
        operation_id="hr_employees_device_mappings",
        responses={200: DeviceMappingsSerializer, **READ_ERRORS},
        tags=TAGS,
        description="Where the person is enrolled, device by device (read-only; no terminal is contacted). Empty until the devices package is installed.",
    )
    @action(detail=True, methods=["get"], url_path="device-mappings")
    def device_mappings(self, request, *args, **kwargs):
        return Response(DeviceMappingsSerializer(employees.device_mappings(self.get_object())).data)

    @extend_schema(
        operation_id="hr_employees_dependencies",
        responses={200: DependenciesSerializer, **READ_ERRORS},
        tags=TAGS,
        description="History attached to the employee (leave, attendance days, raw punches, device mappings) and whether it could be deleted.",
    )
    @action(detail=True, methods=["get"])
    def dependencies(self, request, *args, **kwargs):
        return Response(DependenciesSerializer(employees.dependencies(self.get_object())).data)

    @extend_schema(
        operation_id="hr_employees_reconcile_devices",
        request=ReconcileRequestSerializer,
        responses={200: ReconcileResultSerializer, 503: ErrorSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description=(
            "Reconcile the employee list against every registered device. Preview by default; `apply` needs `confirm` "
            "(400 `confirmation_required`). 503 `devices_unavailable` until the devices package is installed."
        ),
    )
    @action(detail=False, methods=["post"], url_path="reconcile-devices")
    def reconcile_devices(self, request, *args, **kwargs):
        body = ReconcileRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(ReconcileResultSerializer({"result": employees.reconcile_devices(user=request.user, **body.validated_data)}).data)
