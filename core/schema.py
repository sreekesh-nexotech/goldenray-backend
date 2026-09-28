"""OpenAPI description of machine authentication (drf-spectacular extension, registered on import)."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class ServiceTokenScheme(OpenApiAuthenticationExtension):
    target_class = "core.service_credentials.ServiceTokenAuthentication"
    name = "serviceToken"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "fl_<prefix>_<secret>", "description": "Office-agent service token (shown once when issued or rotated)."}
