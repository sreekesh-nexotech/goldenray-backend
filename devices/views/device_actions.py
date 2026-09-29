"""``devices/<uid>/…`` actions: reconciliation views, the refresh request, rehome and ADMS enable/disable."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from devices.serializers.devices import (
    ActionSerializer,
    AdmsEnableSerializer,
    AdmsTokenSerializer,
    DeviceSerializer,
    EmployeeReconciliationSerializer,
    RehomeSerializer,
    UserReconciliationSerializer,
)
from devices.services import devices, roster
from devices.views.common import READ_ERRORS, TAGS, WRITE_ERRORS

TOKEN_WARNING = "Store this now: only its sha256 is kept, so it cannot be shown again. Enabling again issues a new token and invalidates this one."


class DeviceActionsMixin:
    def _device_response(self, device, status=200):
        return Response(DeviceSerializer(devices.devices_queryset().get(pk=device.pk), context=self.get_serializer_context()).data, status=status)

    @extend_schema(
        operation_id="devices_refresh_employees",
        request=None,
        responses={200: EmployeeReconciliationSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description=(
            "Ask the device's office agent to re-read the terminal's user table on its next cycle (the server dials no "
            "terminal) and answer with the categories from the last successful read. 409 `device_inactive`."
        ),
    )
    @action(detail=True, methods=["post"], url_path="refresh-employees")
    def refresh_employees(self, request, *args, **kwargs):
        device = self.get_object()
        refresh = devices.request_user_read(device, user=request.user)
        device = devices.devices_queryset().get(pk=device.pk)
        return Response(EmployeeReconciliationSerializer(devices.reconciliation(device, refresh), context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="devices_employee_reconciliation",
        responses={200: EmployeeReconciliationSerializer, **READ_ERRORS},
        tags=TAGS,
        description="The four operator categories of this device's users, without contacting anything.",
    )
    @action(detail=True, methods=["get"], url_path="employee-reconciliation")
    def employee_reconciliation(self, request, *args, **kwargs):
        return Response(EmployeeReconciliationSerializer(devices.reconciliation(self.get_object()), context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="devices_user_reconciliation",
        responses={200: UserReconciliationSerializer, **READ_ERRORS},
        tags=TAGS,
        description="State of every user mapping on this device as of its last successful read (read-only, idempotent).",
    )
    @action(detail=True, methods=["get"], url_path="user-reconciliation")
    def user_reconciliation(self, request, *args, **kwargs):
        return Response(UserReconciliationSerializer(roster.reconcile(self.get_object())).data)

    @extend_schema(
        operation_id="devices_rehome",
        request=RehomeSerializer,
        responses={200: DeviceSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Bind the device to another agent (or none), optionally moving it to an office; audited with the reason. The only way a device changes agent.",
    )
    @action(detail=True, methods=["post"])
    def rehome(self, request, *args, **kwargs):
        body = RehomeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        device = devices.rehome(
            self.get_object(),
            user=request.user,
            agent=data["agent_uid"],
            office=data.get("office_uid"),
            office_given="office_uid" in data,
            reason=data["reason"],
            expected_version=data.get("expected_version"),
        )
        return self._device_response(device)

    @extend_schema(
        operation_id="devices_adms_enable",
        request=AdmsEnableSerializer,
        responses={200: AdmsTokenSerializer, **WRITE_ERRORS, 503: ErrorSerializer},
        tags=TAGS,
        description="Issue the per-device push token for /iclock/<token>/ (shown once) and set the source allow-list. 409 `device_serial_required` for a device without a serial.",
    )
    @action(detail=True, methods=["post"], url_path="adms/enable")
    def adms_enable(self, request, *args, **kwargs):
        body = AdmsEnableSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        device, token = devices.enable_adms(self.get_object(), user=request.user, allowed_ips=body.validated_data.get("allowed_ips"), expected_version=body.validated_data.get("expected_version"))
        device = devices.devices_queryset().get(pk=device.pk)
        payload = {"device": device, "token": token, "iclock_path": f"/iclock/{token}/", "warning": TOKEN_WARNING}
        return Response(AdmsTokenSerializer(payload, context=self.get_serializer_context()).data)

    @extend_schema(
        operation_id="devices_adms_disable",
        request=ActionSerializer,
        responses={200: DeviceSerializer, **WRITE_ERRORS},
        tags=TAGS,
        description="Withdraw the push token; the terminal's pushes are quarantined from now on.",
    )
    @action(detail=True, methods=["post"], url_path="adms/disable")
    def adms_disable(self, request, *args, **kwargs):
        body = ActionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return self._device_response(devices.disable_adms(self.get_object(), user=request.user, expected_version=body.validated_data.get("expected_version")))
