"""Staff authentication: SimpleJWT RS256 access tokens carrying identity only.

``SessionAwareJWTAuthentication`` is the class every staff view uses (``DEFAULT_AUTHENTICATION_CLASSES``):

* the user is resolved by the ``sub`` claim (user ``uid``) among *live* users and must be active and not waiting
  for a password reset;
* the ``sid`` claim must name a live session of that user (``accounts.services.sessions.is_session_live``) — a
  revoked or expired session invalidates its access tokens on the very next request (version-keyed cache);
* nothing else in the token is trusted: permissions are resolved server-side (``accounts.services.authz``).

On success the principal is attached to the request's audit context (``audit.context``).
"""

from __future__ import annotations

import jwt
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.settings import api_settings

from accounts.services.sessions import SESSION_CLAIM, is_session_live
from audit import context as audit_context


class SessionAwareJWTAuthentication(JWTAuthentication):
    www_authenticate_realm = "api"

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            audit_context.set_actor(result[0], "USER")
        return result

    def get_user(self, validated_token):
        try:
            user_id = validated_token[api_settings.USER_ID_CLAIM]
            session_uid = validated_token[SESSION_CLAIM]
        except KeyError:
            raise InvalidToken(_("Token contained no recognizable user or session identification")) from None
        try:
            user = self.user_model.objects.select_related("role").get(**{api_settings.USER_ID_FIELD: user_id})
        except (self.user_model.DoesNotExist, DjangoValidationError, ValueError, TypeError):
            raise AuthenticationFailed(_("User not found"), code="user_not_found") from None
        if not user.is_active:
            raise AuthenticationFailed(_("User is inactive"), code="user_inactive")
        if user.must_reset_password:
            raise AuthenticationFailed(_("A password reset is required"), code="password_reset_required")
        if not is_session_live(user, session_uid):
            raise AuthenticationFailed(_("The session has ended"), code="session_revoked")
        return user


class StudioTokenAuthentication(SessionAwareJWTAuthentication):
    """Platform RS256 tokens, plus Content Studio (CMS, HS256) access tokens on the views that opt in.

    The Studio token proves who signed in (the CMS signs it with a key shared through ``STUDIO_JWT_SIGNING_KEY``);
    it is mapped to a platform user by its ``email`` claim and nothing else in it is trusted — what the user may
    do still comes from their platform role (``accounts.services.authz``). Disabled while the key is empty, and a
    token whose email matches no live, active platform user is a 401.
    """

    def authenticate(self, request):
        header = self.get_header(request)
        raw = self.get_raw_token(header) if header is not None else None
        if raw is None or _token_algorithm(raw) != "HS256":
            return super().authenticate(request)
        user = self._studio_user(raw)
        audit_context.set_actor(user, "USER")
        return user, None

    def _studio_user(self, raw: str):
        key = (getattr(settings, "STUDIO_JWT_SIGNING_KEY", "") or "").strip()
        if not key:
            raise AuthenticationFailed(_("Studio sign-in is not enabled"), code="studio_auth_disabled")
        try:
            claims = jwt.decode(raw, key, algorithms=["HS256"], leeway=5)
        except jwt.PyJWTError:
            raise InvalidToken(_("Token is invalid or expired")) from None
        email = str(claims.get("email") or "").strip()
        if claims.get("token_type") != "access" or not email:
            raise InvalidToken(_("Not a Studio access token carrying an e-mail"))
        user = self.user_model.objects.select_related("role").filter(email__iexact=email).first()
        if user is None or not user.is_active:
            raise AuthenticationFailed(_("No active platform account for this Studio user"), code="user_not_found")
        if user.must_reset_password:
            raise AuthenticationFailed(_("A password reset is required"), code="password_reset_required")
        return user


def _token_algorithm(raw: str) -> str | None:
    try:
        return jwt.get_unverified_header(raw).get("alg")
    except jwt.PyJWTError:
        return None
