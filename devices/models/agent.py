"""``devices_agent`` (PLAN §2.9): one office agent per office LAN, authenticated by a ``core_service_credential``.

The agent reads the terminals on its LAN (pyzk) and uploads over ``/api/agent/<version>/``. Its token is a platform
service credential (sha256, prefix lookup, shown once) — never stored here. ``agent_version`` is the software version
the agent reports (the PLAN's ``version`` column: that name is the optimistic-locking column of every table, DV-74).

Health is derived from timestamps, never stored: see :mod:`devices.services.health`.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from core.models import BaseModel


class Agent(BaseModel):
    code = models.CharField(max_length=60, help_text="Stable identity, e.g. OFFICE-001-AGENT (unique among live agents).")
    name = models.CharField(max_length=120)
    # Where the agent is filed. SET_NULL: an office going away never deletes the agent (its devices keep their own
    # office — the device assignment is the mapping, the agent's filing is a label).
    office = models.ForeignKey("hr.Office", null=True, blank=True, on_delete=models.SET_NULL, related_name="device_agents")
    # The machine credential. SET_NULL (PLAN "S"): credentials are never hard-deleted, and an agent without one is REVOKED.
    credential = models.ForeignKey("core.ServiceCredential", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    # Self-reported on each heartbeat.
    agent_version = models.CharField(max_length=40, blank=True, default="")
    hostname = models.CharField(max_length=120, blank=True, default="")
    platform = models.CharField(max_length=120, blank=True, default="")
    local_ip = models.GenericIPAddressField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    last_device_contact_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    queued_records = models.PositiveIntegerField(default=0)
    failed_uploads = models.PositiveIntegerField(default=0)

    # Central configuration handed back to the agent on every heartbeat.
    heartbeat_interval_seconds = models.PositiveIntegerField(default=60)
    sync_interval_seconds = models.PositiveIntegerField(default=300)
    offline_after_seconds = models.PositiveIntegerField(default=300)
    degraded_queue_threshold = models.PositiveIntegerField(default=500)
    settings = models.JSONField(default=dict, blank=True, help_text="Free agent settings returned with the configuration.")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "devices_agent"
        ordering = ["code", "id"]
        constraints = [
            models.UniqueConstraint(fields=["code"], condition=Q(deleted_at__isnull=True), name="devices_agent_code_live_uniq"),
            models.CheckConstraint(condition=~Q(code=""), name="devices_agent_code_not_blank"),
            models.CheckConstraint(condition=Q(heartbeat_interval_seconds__gte=10, heartbeat_interval_seconds__lte=3600), name="devices_agent_heartbeat_interval_range"),
            models.CheckConstraint(condition=Q(sync_interval_seconds__gte=30, sync_interval_seconds__lte=86400), name="devices_agent_sync_interval_range"),
            models.CheckConstraint(condition=Q(offline_after_seconds__gte=F("heartbeat_interval_seconds")), name="devices_agent_offline_after_heartbeat"),
            models.CheckConstraint(condition=Q(degraded_queue_threshold__gte=1), name="devices_agent_degraded_threshold_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"
