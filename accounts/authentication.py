"""Staff authentication: SimpleJWT RS256 access tokens carrying identity only (``sub`` = user uid).

``SessionAwareJWTAuthentication`` is the class every staff view uses (``DEFAULT_AUTHENTICATION_CLASSES``). It
resolves the user by ``uid`` among *live, active* users and never trusts anything in the token beyond identity;
permissions are resolved server-side per request (``accounts.services.authz``). The accounts work package extends
it with session revocation checks (``accounts_user_session``).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.settings import api_settings


class SessionAwareJWTAuthentication(JWTAuthentication):
    www_authenticate_realm = "api"

    def get_user(self, validated_token):
        try:
            user_id = validated_token[api_settings.USER_ID_CLAIM]
        except KeyError:
            raise InvalidToken(_("Token contained no recognizable user identification")) from None
        try:
            user = self.user_model.objects.select_related("role").get(**{api_settings.USER_ID_FIELD: user_id})
        except (self.user_model.DoesNotExist, DjangoValidationError, ValueError, TypeError):
            raise AuthenticationFailed(_("User not found"), code="user_not_found") from None
        if not user.is_active:
            raise AuthenticationFailed(_("User is inactive"), code="user_inactive")
        return user
