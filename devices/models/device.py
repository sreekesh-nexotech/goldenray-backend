"""``devices_device`` and ``devices_device_user`` (PLAN §2.9).

A **device** is one attendance terminal. Its identity is the serial the hardware reports (``serial_number``), pinned
by ``expected_serial`` / ``expected_mac`` (what it is required to report); the LAN address is location, never
identity — ``192.168.1.209`` exists in every office. There is no ``is_online`` column: connection health is derived
from timestamps (``last_seen_at`` for agent-delivered terminals, ``adms_last_seen_at`` for pushing ones) by
:mod:`devices.services.health`.

``adms_token_hash`` is the sha256 of the per-device secret path segment of ``/iclock/<device_token>/…``; the
plaintext is shown once by ``POST devices/<uid>/adms/enable/``.

A **device user** is a person as ONE terminal stores them: identity is ``(device, pin)`` (A1). A link to an employee
is per device row; "the same PIN on two terminals" is two rows linked explicitly.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from core.models import BaseModel

MAC_REGEX = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"
SHA256_REGEX = r"^[0-9a-f]{64}$"


class Device(BaseModel):
    class Protocol(models.TextChoices):
        ZK_TCP = "ZK_TCP", "ZK over TCP"
        ZK_UDP = "ZK_UDP", "ZK over UDP"

    class IdentityStatus(models.TextChoices):
        VERIFIED = "VERIFIED", "Verified"
        IDENTITY_MISMATCH = "IDENTITY_MISMATCH", "Identity mismatch"
        UNVERIFIED = "UNVERIFIED", "Unverified"

    class RegistrationState(models.TextChoices):
        NEVER_SEEN = "NEVER_SEEN", "Never seen"
        REGISTERED = "REGISTERED", "Registered"

    name = models.CharField(max_length=120)
    serial_number = models.CharField(max_length=80, null=True, blank=True, help_text="The serial the hardware reported (unique among live devices).")
    expected_serial = models.CharField(max_length=80, null=True, blank=True, help_text="The serial the terminal is required to report (identity pin).")
    expected_mac = models.CharField(max_length=17, blank=True, default="", help_text="The MAC the terminal is required to report (aa:bb:cc:dd:ee:ff).")
    mac_address = models.CharField(max_length=17, blank=True, default="", help_text="The MAC the hardware reported.")
    # Where the terminal stands. SET_NULL: the device (and its punch history) outlives an office.
    office = models.ForeignKey("hr.Office", null=True, blank=True, on_delete=models.SET_NULL, related_name="devices")
    # The agent that carries its punches. SET_NULL: removing an agent leaves the device unassigned, never deleted.
    agent = models.ForeignKey("devices.Agent", null=True, blank=True, on_delete=models.SET_NULL, related_name="devices")
    ip_address = models.GenericIPAddressField(null=True, blank=True, help_text="LAN address; null until an agent discovers it.")
    port = models.PositiveIntegerField(default=4370)
    protocol = models.CharField(max_length=6, choices=Protocol.choices, default=Protocol.ZK_TCP)
    comm_password = models.PositiveIntegerField(default=0, help_text="The terminal's communication key (never returned by the API).")
    timeout_seconds = models.PositiveSmallIntegerField(default=15)

    # Identity read back from the hardware.
    model = models.CharField(max_length=120, blank=True, default="")
    firmware_version = models.CharField(max_length=120, blank=True, default="")
    platform = models.CharField(max_length=120, blank=True, default="")

    # ADMS push (the terminal connects to /iclock/<device_token>/…, flag ADMS_RECEIVER).
    adms_enabled = models.BooleanField(default=False)
    adms_token_hash = models.CharField(max_length=64, null=True, blank=True, help_text="sha256 of the /iclock/ path token.")
    adms_allowed_ips = models.JSONField(default=list, blank=True, help_text="CIDRs the terminal may push from; empty = any address.")
    adms_registration_state = models.CharField(max_length=20, choices=RegistrationState.choices, default=RegistrationState.NEVER_SEEN)
    adms_last_seen_at = models.DateTimeField(null=True, blank=True)
    adms_last_handshake_at = models.DateTimeField(null=True, blank=True)
    adms_last_push_at = models.DateTimeField(null=True, blank=True)
    adms_last_command_poll_at = models.DateTimeField(null=True, blank=True)
    adms_source_ip = models.GenericIPAddressField(null=True, blank=True)
    adms_request_count = models.PositiveIntegerField(default=0)
    adms_options = models.JSONField(default=dict, blank=True, help_text="What the terminal volunteered at its last handshake.")
    # What the terminal's own screen shows as its push server (an observation, never written to the device; DV-75).
    adms_server = models.CharField(max_length=120, blank=True, default="")
    adms_port = models.PositiveIntegerField(null=True, blank=True)

    # Identity check result; a mismatch is sticky until a correct terminal answers.
    identity_status = models.CharField(max_length=20, choices=IdentityStatus.choices, default=IdentityStatus.UNVERIFIED)
    identity_message = models.TextField(blank=True, default="")
    identity_checked_at = models.DateTimeField(null=True, blank=True)

    # Contact and counters (health is derived from these; no is_online column).
    last_seen_at = models.DateTimeField(null=True, blank=True, help_text="Last time an agent reached the terminal.")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_punch_at = models.DateTimeField(null=True, blank=True, help_text="Newest punch received (device wall clock in the office zone).")
    last_error = models.TextField(blank=True, default="")
    clock_offset_seconds = models.IntegerField(null=True, blank=True, help_text="Terminal clock minus real time, measured on each info read (A10; never corrected).")
    user_count = models.PositiveIntegerField(default=0)
    attendance_count = models.PositiveIntegerField(default=0)
    device_info = models.JSONField(default=dict, blank=True)
    # A staff request that the agent re-read the terminal's user table on its next cycle (devices.sync; DV-75).
    users_read_requested_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "devices_device"
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["serial_number"], condition=Q(deleted_at__isnull=True, serial_number__isnull=False), name="devices_device_serial_live_uniq"),
            models.UniqueConstraint(fields=["expected_serial"], condition=Q(deleted_at__isnull=True, expected_serial__isnull=False), name="devices_device_expected_serial_live_uniq"),
            models.UniqueConstraint(fields=["adms_token_hash"], condition=Q(adms_token_hash__isnull=False), name="devices_device_adms_token_uniq"),
            models.CheckConstraint(condition=~Q(name=""), name="devices_device_name_not_blank"),
            models.CheckConstraint(condition=Q(serial_number__isnull=True) | ~Q(serial_number=""), name="devices_device_serial_not_blank"),
            models.CheckConstraint(condition=Q(expected_serial__isnull=True) | ~Q(expected_serial=""), name="devices_device_expected_serial_not_blank"),
            models.CheckConstraint(condition=Q(expected_mac="") | Q(expected_mac__regex=MAC_REGEX), name="devices_device_expected_mac_format"),
            models.CheckConstraint(condition=Q(mac_address="") | Q(mac_address__regex=MAC_REGEX), name="devices_device_mac_format"),
            models.CheckConstraint(condition=Q(port__gte=1, port__lte=65535), name="devices_device_port_range"),
            models.CheckConstraint(condition=Q(timeout_seconds__gte=1, timeout_seconds__lte=120), name="devices_device_timeout_range"),
            models.CheckConstraint(condition=Q(protocol__in=["ZK_TCP", "ZK_UDP"]), name="devices_device_protocol_valid"),
            models.CheckConstraint(condition=Q(identity_status__in=["VERIFIED", "IDENTITY_MISMATCH", "UNVERIFIED"]), name="devices_device_identity_status_valid"),
            models.CheckConstraint(condition=Q(adms_registration_state__in=["NEVER_SEEN", "REGISTERED"]), name="devices_device_adms_registration_valid"),
            models.CheckConstraint(condition=Q(adms_token_hash__isnull=True) | Q(adms_token_hash__regex=SHA256_REGEX), name="devices_device_adms_token_hash_format"),
            # A terminal expected to push has a token to push with.
            models.CheckConstraint(condition=Q(adms_enabled=False) | Q(adms_token_hash__isnull=False), name="devices_device_adms_enabled_has_token"),
            # A device must be findable: an address to reach it at, or the serial it will announce.
            models.CheckConstraint(condition=Q(ip_address__isnull=False) | Q(expected_serial__isnull=False) | Q(serial_number__isnull=False), name="devices_device_locatable"),
            models.CheckConstraint(condition=Q(adms_port__isnull=True) | Q(adms_port__gte=1, adms_port__lte=65535), name="devices_device_adms_port_range"),
        ]
        indexes = [models.Index(fields=["office", "is_active"], name="devices_device_office_idx"), models.Index(fields=["agent", "is_active"], name="devices_device_agent_idx")]

    def __str__(self) -> str:
        serial = self.serial_number or self.expected_serial
        return f"{self.name} · {serial}" if serial else self.name


class DeviceUser(BaseModel):
    # A true child of the terminal: CASCADE (devices are only soft-deleted in practice).
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="device_users")
    pin = models.CharField(max_length=80, help_text="The terminal's user id, always a string ('1' and 'EMP001' both occur).")
    device_uid = models.IntegerField(null=True, blank=True, help_text="The terminal's internal row id.")
    name = models.CharField(max_length=150, blank=True, default="")
    privilege = models.SmallIntegerField(null=True, blank=True, help_text="The terminal's privilege byte (unrelated to platform roles).")
    card = models.CharField(max_length=40, blank=True, default="")
    group_id = models.CharField(max_length=40, blank=True, default="")
    has_password = models.BooleanField(default=False)
    # Who this terminal user is. SET_NULL: removing an employee unlinks, never deletes the enrolment record.
    employee = models.ForeignKey("hr.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="device_users")
    raw_payload = models.JSONField(default=dict, blank=True)
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "devices_device_user"
        ordering = ["device_id", "pin", "id"]
        constraints = [
            models.UniqueConstraint(fields=["device", "pin"], condition=Q(deleted_at__isnull=True), name="devices_device_user_pin_live_uniq"),
            models.CheckConstraint(condition=~Q(pin=""), name="devices_device_user_pin_not_blank"),
        ]
        indexes = [models.Index(fields=["pin"], name="devices_device_user_pin_idx")]

    def __str__(self) -> str:
        return f"{self.pin} on {self.device_id}"
