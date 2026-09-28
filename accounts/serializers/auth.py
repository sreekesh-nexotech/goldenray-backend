"""Auth endpoint shapes (``auth/…``). Input serializers validate shape only; the services own every rule."""

from rest_framework import serializers

from accounts.services.passwords import MAX_PASSWORD_LENGTH

PERMISSIONS_FIELD_HELP = "`{module: [actions]}` from the permission registry."
SCOPES_FIELD_HELP = "`{module: scope}` — the record scope per permitted module."


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(max_length=MAX_PASSWORD_LENGTH, trim_whitespace=False, write_only=True, style={"input_type": "password"})


class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField(help_text="RS256 access token (15 minutes). Send as `Authorization: Bearer <access>`.")
    refresh = serializers.CharField(help_text="Refresh token (rotated on every use; store server-side in the BFF).")
    token_type = serializers.CharField(default="Bearer")
    access_expires_at = serializers.DateTimeField()
    refresh_expires_at = serializers.DateTimeField()
    session_uid = serializers.UUIDField()

    @staticmethod
    def from_tokens(tokens) -> dict:
        return {
            "access": tokens.access,
            "refresh": tokens.refresh,
            "token_type": "Bearer",
            "access_expires_at": tokens.access_expires_at,
            "refresh_expires_at": tokens.refresh_expires_at,
            "session_uid": tokens.session.uid,
        }


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField(max_length=4096, trim_whitespace=True)


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(max_length=MAX_PASSWORD_LENGTH, trim_whitespace=False, write_only=True, style={"input_type": "password"})
    new_password = serializers.CharField(max_length=MAX_PASSWORD_LENGTH, trim_whitespace=False, write_only=True, style={"input_type": "password"})


class PasswordChangeResultSerializer(serializers.Serializer):
    other_sessions_ended = serializers.IntegerField(help_text="How many of your other sessions were signed out.")


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)


class PasswordResetSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=256, help_text="The token from the reset or invitation link.")
    new_password = serializers.CharField(max_length=MAX_PASSWORD_LENGTH, trim_whitespace=False, write_only=True, style={"input_type": "password"})


class DetailSerializer(serializers.Serializer):
    detail = serializers.CharField()


class MeRoleSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    slug = serializers.CharField()
    name = serializers.CharField()
    is_system = serializers.BooleanField()


class MeUserSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    email = serializers.EmailField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    full_name = serializers.CharField()
    phone_e164 = serializers.CharField()
    title = serializers.CharField()
    last_login_at = serializers.DateTimeField(allow_null=True)
    password_changed_at = serializers.DateTimeField(allow_null=True)


class MeSerializer(serializers.Serializer):
    """``GET auth/me/`` — who the caller is and what they may do (drives the Studio navigation)."""

    user = MeUserSerializer()
    role = MeRoleSerializer()
    permissions = serializers.DictField(child=serializers.ListField(child=serializers.CharField()), help_text=PERMISSIONS_FIELD_HELP)
    scopes = serializers.DictField(child=serializers.CharField(), help_text=SCOPES_FIELD_HELP)
    session_uid = serializers.UUIDField(allow_null=True)

    @staticmethod
    def build(user, grants, session_uid) -> dict:
        return {
            "user": {
                "uid": user.uid,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "full_name": user.get_full_name(),
                "phone_e164": user.phone_e164,
                "title": user.title,
                "last_login_at": user.last_login_at,
                "password_changed_at": user.password_changed_at,
            },
            "role": {"uid": user.role.uid, "slug": user.role.slug, "name": user.role.name, "is_system": user.role.is_system},
            **grants.as_dict(),
            "session_uid": session_uid,
        }


class SessionSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    created_at = serializers.DateTimeField(help_text="Sign-in time.")
    last_refreshed_at = serializers.DateTimeField(source="updated_at")
    expires_at = serializers.DateTimeField()
    ip = serializers.IPAddressField(allow_null=True)
    user_agent = serializers.CharField()
    current = serializers.SerializerMethodField(help_text="True for the session making this request.")

    def get_current(self, session) -> bool:
        return str(session.uid) == str(self.context.get("current_session_uid") or "")
