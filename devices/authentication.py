"""Office-agent authentication for ``/api/agent/<version>/``: a platform service token bound to a live, active agent.

The token is verified by :class:`core.service_credentials.ServiceTokenAuthentication` (sha256, prefix lookup,
constant-time — one hash per request, not eSSL's bcrypt per request). The credential must then belong to a live
agent that is active: a disabled or deleted agent is refused like a revoked token (401), so the agent treats it as
"credential refused" and keeps its queue.
"""

from __future__ import annotations

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions

from core.service_credentials import ServiceTokenAuthentication
from devices.models import Agent


class AgentTokenAuthentication(ServiceTokenAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        principal, credential = result
        agent = Agent.objects.select_related("office", "credential").filter(credential=credential).first()
        if agent is None or not agent.is_active:
            raise exceptions.AuthenticationFailed("This agent is disabled or no longer exists.")
        request._request.devices_agent = agent
        return principal, credential


def agent_of(request) -> Agent:
    return request._request.devices_agent


class AgentTokenScheme(OpenApiAuthenticationExtension):
    """The same bearer scheme as ``core.schema.ServiceTokenScheme`` (an agent token is a service token)."""

    target_class = "devices.authentication.AgentTokenAuthentication"
    name = "serviceToken"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "fl_<prefix>_<secret>", "description": "Office-agent service token (shown once when issued or rotated)."}
