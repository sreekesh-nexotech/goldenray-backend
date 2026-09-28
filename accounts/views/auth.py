"""``auth/…`` — sign-in, token refresh, sign-out, the caller's identity, passwords and sessions (PLAN §3.4).

Anonymous endpoints (login, refresh, logout, password reset) ignore any ``Authorization`` header (an expired access
token must not block a refresh) and are throttled per trusted client IP: ``login`` for sign-in and password reset,
``token_refresh`` for refresh/logout (DV-8). The rest need a valid access token and are open to every signed-in
user — they only ever touch the caller's own account (no module permission applies).
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import permissions, status
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers.auth import (
    DetailSerializer,
    LoginSerializer,
    MeSerializer,
    PasswordChangeResultSerializer,
    PasswordChangeSerializer,
    PasswordResetRequestSerializer,
    PasswordResetSerializer,
    RefreshSerializer,
    SessionSerializer,
    TokenPairSerializer,
)
from accounts.services import auth, sessions
from accounts.services.authz import get_grants
from core.serializers import ErrorSerializer
from core.views import ListModelMixin
from flarize.client_ip import get_client_ip

TAGS = ["auth"]
RESET_REQUESTED = "If the address belongs to an active account, a reset link has been sent."


def _client(request) -> tuple[str | None, str]:
    return get_client_ip(request) or None, request.headers.get("User-Agent", "")[:512]


def _current_session_uid(request):
    token = getattr(request, "auth", None)
    return token.get(sessions.SESSION_CLAIM) if token is not None and hasattr(token, "get") else None


class AnonymousAuthView(APIView):
    authentication_classes: list = []
    permission_classes = [permissions.AllowAny]
    throttle_scope = "login"

    def get_throttle_ident(self, request) -> str:
        return f"ip:{get_client_ip(request) or 'unknown'}"


class SelfServiceView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "staff"


class LoginView(AnonymousAuthView):
    @extend_schema(
        operation_id="auth_login",
        request=LoginSerializer,
        responses={200: TokenPairSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 429: ErrorSerializer},
        tags=TAGS,
        description="`invalid_credentials` (401), `password_reset_required` (403), `login_locked` (429, `Retry-After`).",
    )
    def post(self, request, *args, **kwargs):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ip, user_agent = _client(request)
        tokens = auth.login(serializer.validated_data["email"], serializer.validated_data["password"], ip=ip, user_agent=user_agent)
        return Response(TokenPairSerializer.from_tokens(tokens))


class RefreshView(AnonymousAuthView):
    throttle_scope = "token_refresh"

    @extend_schema(
        operation_id="auth_refresh",
        request=RefreshSerializer,
        responses={200: TokenPairSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer},
        tags=TAGS,
        description="Rotates the refresh token (the old one stops working). 401 codes: `token_invalid`, `session_revoked`, `session_expired`, "
        "`refresh_token_rotated`, `refresh_token_reused` (the session is ended), `user_inactive`; 403 `password_reset_required`.",
    )
    def post(self, request, *args, **kwargs):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ip, user_agent = _client(request)
        return Response(TokenPairSerializer.from_tokens(auth.refresh(serializer.validated_data["refresh"], ip=ip, user_agent=user_agent)))


class LogoutView(AnonymousAuthView):
    throttle_scope = "token_refresh"

    @extend_schema(
        operation_id="auth_logout",
        request=RefreshSerializer,
        responses={204: OpenApiResponse(description="Signed out."), 400: ErrorSerializer, 401: ErrorSerializer},
        tags=TAGS,
    )
    def post(self, request, *args, **kwargs):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        auth.logout(serializer.validated_data["refresh"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetRequestView(AnonymousAuthView):
    @extend_schema(
        operation_id="auth_password_reset_request",
        request=PasswordResetRequestSerializer,
        responses={200: DetailSerializer, 400: ErrorSerializer, 429: ErrorSerializer},
        tags=TAGS,
        description="Always 200, whether or not the address has an account.",
    )
    def post(self, request, *args, **kwargs):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        auth.request_password_reset(serializer.validated_data["email"], ip=_client(request)[0])
        return Response({"detail": RESET_REQUESTED})


class PasswordResetView(AnonymousAuthView):
    @extend_schema(
        operation_id="auth_password_reset",
        request=PasswordResetSerializer,
        responses={204: OpenApiResponse(description="Password set; every session of the account was signed out."), 400: ErrorSerializer, 429: ErrorSerializer},
        tags=TAGS,
        description="Completes a reset or an invitation. 400 codes: `reset_token_invalid`, `reset_token_expired`, `validation_error` (password policy).",
    )
    def post(self, request, *args, **kwargs):
        serializer = PasswordResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        auth.reset_password(serializer.validated_data["token"], serializer.validated_data["new_password"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(SelfServiceView):
    @extend_schema(operation_id="auth_me", responses={200: MeSerializer, 401: ErrorSerializer}, tags=TAGS)
    def get(self, request, *args, **kwargs):
        return Response(MeSerializer.build(request.user, get_grants(request.user), _current_session_uid(request)))


class PasswordChangeView(SelfServiceView):
    @extend_schema(
        operation_id="auth_password_change",
        request=PasswordChangeSerializer,
        responses={200: PasswordChangeResultSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 429: ErrorSerializer},
        tags=TAGS,
        description="Keeps the current session and signs out every other one. 400 `invalid_current_password` counts towards the sign-in lockout.",
    )
    def post(self, request, *args, **kwargs):
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ended = auth.change_password(
            request.user,
            current_password=serializer.validated_data["current_password"],
            new_password=serializer.validated_data["new_password"],
            session_uid=_current_session_uid(request),
            ip=_client(request)[0],
        )
        return Response({"other_sessions_ended": ended})


class SessionListView(ListModelMixin, GenericAPIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_scope = "staff"
    serializer_class = SessionSerializer
    filter_backends: list = []

    def get_queryset(self):
        return sessions.live_sessions(self.request.user)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "current_session_uid": _current_session_uid(self.request)}

    @extend_schema(operation_id="auth_sessions_list", responses={200: SessionSerializer(many=True), 401: ErrorSerializer}, tags=TAGS)
    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


class SessionDetailView(SelfServiceView):
    @extend_schema(operation_id="auth_sessions_revoke", responses={204: OpenApiResponse(description="Session signed out."), 401: ErrorSerializer, 404: ErrorSerializer}, tags=TAGS)
    def delete(self, request, uid, *args, **kwargs):
        session = sessions.get_own_session(request.user, uid)
        sessions.revoke_session(session, user=request.user, reason="user_signed_out_session")
        return Response(status=status.HTTP_204_NO_CONTENT)
