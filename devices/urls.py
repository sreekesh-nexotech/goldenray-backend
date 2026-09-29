"""Devices URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``devices/`` (terminals, ``devices/device-users/``, ``devices/agents/``, ``devices/protocol-mappings/``,
``devices/adms/…``); the agent protocol (``config/``, ``heartbeat/``, ``devices/…``, ``sync/…``, ``sync-status/``);
``/iclock/<device_token>/`` terminals (plain Django views, flag ADMS_RECEIVER).
"""

from django.urls import path, re_path
from rest_framework.routers import SimpleRouter

from devices.views import agent_api, iclock
from devices.views.agents import AgentViewSet
from devices.views.device_users import DeviceUserViewSet
from devices.views.devices import DeviceViewSet
from devices.views.protocol import AdmsRequestViewSet, AdmsStatusView, AdmsUnknownDeviceViewSet, ProtocolMappingViewSet

router = SimpleRouter(trailing_slash=True)
router.register("devices/device-users", DeviceUserViewSet, basename="devices-device-users")
router.register("devices/agents", AgentViewSet, basename="devices-agents")
router.register("devices/protocol-mappings", ProtocolMappingViewSet, basename="devices-protocol-mappings")
router.register("devices/adms/requests", AdmsRequestViewSet, basename="devices-adms-requests")
router.register("devices/adms/unknown-devices", AdmsUnknownDeviceViewSet, basename="devices-adms-unknown-devices")
router.register("devices", DeviceViewSet, basename="devices")

staff_urlpatterns = [path("devices/adms/status/", AdmsStatusView.as_view(), name="devices-adms-status"), *router.urls]
public_urlpatterns: list = []
agent_urlpatterns = [
    path("config/", agent_api.ConfigView.as_view(), name="agent-config"),
    path("heartbeat/", agent_api.HeartbeatView.as_view(), name="agent-heartbeat"),
    path("devices/announce/", agent_api.AnnounceView.as_view(), name="agent-devices-announce"),
    path("devices/identity-mismatch/", agent_api.IdentityMismatchView.as_view(), name="agent-devices-identity-mismatch"),
    path("devices/discovery/", agent_api.DiscoveryView.as_view(), name="agent-devices-discovery"),
    path("sync/users/", agent_api.SyncUsersView.as_view(), name="agent-sync-users"),
    path("sync/attendance/", agent_api.SyncAttendanceView.as_view(), name="agent-sync-attendance"),
    path("sync-status/", agent_api.SyncStatusView.as_view(), name="agent-sync-status"),
]
customer_urlpatterns: list = []
# Mounted at /iclock/<device_token>/ (flag ADMS_RECEIVER). Firmware asks for cdata, getrequest, devicecmd, registry and
# ping, sometimes with ``.aspx`` or a trailing slash; anything else is recorded by the catch-all.
iclock_urlpatterns = [
    re_path(r"^(?P<endpoint>cdata|getrequest|devicecmd|registry|ping)(?:\.aspx)?/?$", iclock.receive, name="iclock-endpoint"),
    re_path(r"^(?P<endpoint>.*)$", iclock.receive, name="iclock-other"),
]
