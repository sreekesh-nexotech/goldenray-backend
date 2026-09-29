"""Customer surface ``/api/customer/v1/inspection-approvals/<token>/`` (PLAN §3.4): the signed link is the capability,
the one-time code to the approval's phone proves the customer. Throttle scope ``customer``; responses are
``private, no-store`` (personal data behind a capability URL is never cached by a proxy)."""

from __future__ import annotations

from django.utils.cache import add_never_cache_headers, patch_cache_control
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from core.serializers import ErrorSerializer
from core.views import CustomerAPIView
from flarize.client_ip import get_client_ip
from site_inspections.serializers.customer import ApprovalSummarySerializer, OtpSentSerializer, RespondedSerializer, RespondSerializer
from site_inspections.services import approvals

TAGS = ["inspection-approvals"]
ERRORS = {400: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer, 410: ErrorSerializer, 429: ErrorSerializer}


def _private(response: Response) -> Response:
    add_never_cache_headers(response)
    patch_cache_control(response, private=True)
    return response


class ApprovalSummaryView(CustomerAPIView):
    @extend_schema(
        operation_id="inspection_approvals_retrieve",
        responses={200: ApprovalSummarySerializer, 404: ErrorSerializer, 429: ErrorSerializer},
        tags=TAGS,
        description="The proposed installation locations to approve.",
    )
    def get(self, request, token: str, *args, **kwargs):
        approval = approvals.resolve(token)
        body = approvals.summary(approval, version=request.version, absolute=request.build_absolute_uri)
        return _private(Response(ApprovalSummarySerializer(body).data))


class ApprovalSendOtpView(CustomerAPIView):
    @extend_schema(
        operation_id="inspection_approvals_send_otp",
        request=None,
        responses={200: OtpSentSerializer, 503: ErrorSerializer, **ERRORS},
        tags=TAGS,
        description="Sends a one-time code to the phone on the approval.",
    )
    def post(self, request, token: str, *args, **kwargs):
        body = approvals.send_customer_otp(token, ip=get_client_ip(request))
        return _private(Response(OtpSentSerializer(body).data))


class ApprovalRespondView(CustomerAPIView):
    @extend_schema(
        operation_id="inspection_approvals_respond",
        request={"multipart/form-data": RespondSerializer, "application/json": RespondSerializer},
        responses={200: RespondedSerializer, 503: ErrorSerializer, **ERRORS},
        tags=TAGS,
        description="APPROVED or REJECTED (a rejection needs a comment), with the one-time code; optional drawn signature.",
    )
    def post(self, request, token: str, *args, **kwargs):
        serializer = RespondSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        approval = approvals.respond(token, code=data["code"], decision=data["decision"], comment=data.get("comment", ""), signature=data.get("signature"), ip=get_client_ip(request))
        return _private(Response(RespondedSerializer(approval).data))
