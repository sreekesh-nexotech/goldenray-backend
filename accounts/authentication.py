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
