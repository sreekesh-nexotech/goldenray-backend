"""Website endpoints (``/api/public/v1/``): ``otp/send/``, ``otp/verify/``, ``leads/``, ``affiliate-applications/``,
``warranty-requests/``, ``installations/``, ``installations/stats/``.

Anonymous, no authentication classes. POSTs honour ``Idempotency-Key`` (24 h replay) and are throttled
(``otp`` per phone + ``otp_ip`` per IP on both OTP endpoints, ``public_write`` on the forms); GETs are cached
(version-keyed, ``ETag``/304, ``Cache-Control: public, max-age=60``) and throttled ``public_read``.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response

from core.idempotency import idempotent
from core.serializers import ErrorSerializer
from core.views import ListModelMixin, PublicAPIView, PublicGenericViewSet
from flarize.cache_utils import CachedResponseMixin, cache_response
from flarize.client_ip import get_client_ip
from leads.models import OtpRequest
from leads.serializers.public import (
    AffiliateReceiptSerializer,
    AffiliateSubmitSerializer,
    InstallationQuerySerializer,
    InstallationStatsQuerySerializer,
    InstallationStatsSerializer,
    LeadReceiptSerializer,
    LeadSubmitSerializer,
    OtpSendResponseSerializer,
    OtpSendSerializer,
    OtpVerifyResponseSerializer,
    OtpVerifySerializer,
    PublicInstallationSerializer,
    WarrantyReceiptSerializer,
    WarrantySubmitSerializer,
)
from leads.services import installations, intake, otp
from leads.views.throttles import OtpIpThrottle, OtpPhoneThrottle, OtpVerifyPhoneThrottle

TAGS = ["public"]
IDEMPOTENCY = OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=False, description="8–128 characters; a retry with the same key and body replays the first response for 24 h.")
_WRITE_ERRORS = {400: ErrorSerializer, 409: ErrorSerializer, 422: ErrorSerializer, 429: ErrorSerializer}


class OtpSendView(PublicAPIView):
    throttle_classes = [OtpPhoneThrottle, OtpIpThrottle]

    @extend_schema(
        operation_id="public_otp_send",
        request=OtpSendSerializer,
        responses={200: OtpSendResponseSerializer, **_WRITE_ERRORS, 503: ErrorSerializer},
        parameters=[IDEMPOTENCY],
        tags=TAGS,
        auth=[],
        description="Sends a one-time code by SMS (Twilio Verify) to an Indian mobile number. 429 `otp_limit_reached`, 503 `otp_unavailable`.",
    )
    @idempotent("leads.otp_send")
    def post(self, request, *args, **kwargs):
        serializer = OtpSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sent = otp.send_code(phone_e164=serializer.validated_data["phone"], purpose=OtpRequest.Purpose.LEAD, ip=get_client_ip(request))
        return Response(OtpSendResponseSerializer({"status": "pending", "phone": sent.phone_e164, "expires_at": sent.expires_at}).data)


class OtpVerifyView(PublicAPIView):
    throttle_classes = [OtpVerifyPhoneThrottle, OtpIpThrottle]

    @extend_schema(
        operation_id="public_otp_verify",
        request=OtpVerifySerializer,
        responses={200: OtpVerifyResponseSerializer, **_WRITE_ERRORS, 503: ErrorSerializer},
        parameters=[IDEMPOTENCY],
        tags=TAGS,
        auth=[],
        description="Checks the code; returns the verification token `POST leads` needs. 400 `otp_invalid` / `otp_expired`, 429 `otp_attempts_exceeded`.",
    )
    @idempotent("leads.otp_verify")
    def post(self, request, *args, **kwargs):
        serializer = OtpVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        verification = otp.verify_code(phone_e164=serializer.validated_data["phone"], code=serializer.validated_data["code"], ip=get_client_ip(request))
        return Response(OtpVerifyResponseSerializer({"status": "approved", "verification_token": verification.token, "expires_at": verification.expires_at}).data)


class LeadSubmitView(PublicAPIView):
    @extend_schema(
        operation_id="public_leads_create",
        request=LeadSubmitSerializer,
        responses={201: LeadReceiptSerializer, **_WRITE_ERRORS},
        parameters=[IDEMPOTENCY],
        tags=TAGS,
        auth=[],
        description=(
            "Every website form. A submission with a phone number needs `verification_token` from `otp/verify` for that number "
            "(400 `verification_required` / `verification_invalid` / `verification_expired` / `verification_mismatch`)."
        ),
    )
    @idempotent("leads.submit")
    def post(self, request, *args, **kwargs):
        serializer = LeadSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        token = data.pop("verification_token", "")
        lead = intake.submit_lead(data=data, ip=get_client_ip(request), verification_token=token)
        return Response(LeadReceiptSerializer(lead).data, status=status.HTTP_201_CREATED)


class AffiliateSubmitView(PublicAPIView):
    @extend_schema(
        operation_id="public_affiliate_applications_create",
        request=AffiliateSubmitSerializer,
        responses={201: AffiliateReceiptSerializer, **_WRITE_ERRORS},
        parameters=[IDEMPOTENCY],
        tags=TAGS,
        auth=[],
    )
    @idempotent("leads.affiliate_submit")
    def post(self, request, *args, **kwargs):
        serializer = AffiliateSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        application = intake.submit_affiliate_application(data=serializer.validated_data)
        return Response(AffiliateReceiptSerializer(application).data, status=status.HTTP_201_CREATED)


class WarrantySubmitView(PublicAPIView):
    @extend_schema(
        operation_id="public_warranty_requests_create", request=WarrantySubmitSerializer, responses={201: WarrantyReceiptSerializer, **_WRITE_ERRORS}, parameters=[IDEMPOTENCY], tags=TAGS, auth=[]
    )
    @idempotent("leads.warranty_submit")
    def post(self, request, *args, **kwargs):
        serializer = WarrantySubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        warranty = intake.submit_warranty_request(data=serializer.validated_data)
        return Response(WarrantyReceiptSerializer(warranty).data, status=status.HTTP_201_CREATED)


class PublicInstallationViewSet(CachedResponseMixin, ListModelMixin, PublicGenericViewSet):
    """``installations/?pincode=&district=`` — the showcase map (completed showcase installations, no personal data)."""

    serializer_class = PublicInstallationSerializer
    filter_backends: list = []
    cache_namespaces = (installations.CACHE_NAMESPACE, "media")

    def get_queryset(self):
        query = InstallationQuerySerializer(data=self.request.query_params)
        query.is_valid(raise_exception=True)
        return installations.showcase_queryset(pincode=query.validated_data.get("pincode"), district=query.validated_data.get("district"))

    @extend_schema(
        operation_id="public_installations_list",
        parameters=[OpenApiParameter("pincode", str, description="Exact pincode."), OpenApiParameter("district", str, description="District (any letter case).")],
        tags=TAGS,
        auth=[],
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)


class InstallationStatsView(PublicAPIView):
    @extend_schema(
        operation_id="public_installations_stats",
        parameters=[OpenApiParameter("pincode", str, required=True, description="The pincode as typed (the legacy endpoint accepted any text).")],
        responses={200: InstallationStatsSerializer, 400: ErrorSerializer},
        tags=TAGS,
        auth=[],
        description="Completed installations in the pincode, in its district and in its district this year (the legacy `installation-stats`).",
    )
    @cache_response(namespaces=[installations.CACHE_NAMESPACE], ttl=300)
    def get(self, request, *args, **kwargs):
        query = InstallationStatsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(InstallationStatsSerializer(installations.installation_stats(query.validated_data["pincode"])).data)
