"""OpenAPI description of staff authentication (drf-spectacular extension, registered on import)."""

from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme


class SessionAwareJWTScheme(SimpleJWTScheme):
    target_class = "accounts.authentication.SessionAwareJWTAuthentication"
    name = "staffJWT"
