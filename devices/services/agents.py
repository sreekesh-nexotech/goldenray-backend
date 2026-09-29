"""``devices/agents/`` — office agents on platform service credentials (module ``devices``).

* **Credentials** are ``core_service_credential`` rows (kind AGENT, sha256 of the token, prefix lookup, bound to the
  agent). Creating an agent or rotating its token returns the plaintext **once** (``fl_<prefix>_<secret>``); only the
  prefix is ever shown again. Rotation replaces the token (the old one stops working at once); a revoked credential
  cannot be rotated, so rotating a revoked agent issues a fresh credential and re-activates it. Revoking withdraws the
  credential and disables the agent; its devices, logs and punches stay.
* **Config download** (PLAN §5.7 "Download config"): the create/rotate response also carries a one-time download
  key; ``GET …/config-download/?download=<key>`` returns ``agent.ini`` with the token in it, exactly once, within
  ``DEVICES_AGENT_CONFIG_TTL_SECONDS`` (600). The plaintext token waits for that download Fernet-encrypted in the
  cache, never in the database; a second download (or a late one) is 410 ``download_expired``.
* An agent with live devices bound to it cannot be deleted (409 ``agent_in_use``): rehome the devices or revoke it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from core import service_credentials
from core.errors import Conflict, DomainError
from core.models import ServiceCredential
from core.services import stamp_create
from devices.models import Agent, Device
from devices.services.common import NS_AGENTS, bump_devices, lock, now, setting, unique_conflict
from flarize.crypto import DecryptionError, decrypt_str, encrypt_str

EDITABLE_FIELDS = ("name", "office", "heartbeat_interval_seconds", "sync_interval_seconds", "offline_after_seconds", "degraded_queue_threshold", "settings", "notes")
SNAPSHOT_FIELDS = ("code", *EDITABLE_FIELDS, "is_active", "credential")
UNIQUE = {"devices_agent_code_live_uniq": ("agent_code_taken", "code", "Another agent already uses this code.")}
CONFIG_CACHE_PREFIX = "devices:agent-config:"
AGENT_API_VERSION = "v1"


def agents_queryset():
    return Agent.objects.select_related("office", "credential").order_by("code", "id")


def agent_snapshot(agent: Agent) -> dict:
    return snapshot(agent, SNAPSHOT_FIELDS)


def _check_intervals(values: dict, agent: Agent | None = None) -> None:
    def current(name, default):
        return values.get(name, getattr(agent, name) if agent is not None else default)

    heartbeat = current("heartbeat_interval_seconds", 60)
    offline = current("offline_after_seconds", 300)
    errors = {}
    if not 10 <= heartbeat <= 3600:
        errors["heartbeat_interval_seconds"] = ["Must be between 10 and 3600 seconds."]
    if not 30 <= current("sync_interval_seconds", 300) <= 86400:
        errors["sync_interval_seconds"] = ["Must be between 30 and 86400 seconds."]
    if offline < heartbeat:
        errors["offline_after_seconds"] = ["Must be at least the heartbeat interval (an agent is judged offline only after missing a heartbeat)."]
    if current("degraded_queue_threshold", 500) < 1:
        errors["degraded_queue_threshold"] = ["Must be at least 1."]
    if "settings" in values and not isinstance(values["settings"], dict):
        errors["settings"] = ["Must be a JSON object."]
    if errors:
        raise DomainError("validation_error", "Invalid input.", errors=errors)


def _normalise(values: dict) -> dict:
    if "code" in values:
        values["code"] = (values["code"] or "").strip()
        if not values["code"]:
            raise DomainError("validation_error", "Invalid input.", errors={"code": ["This field may not be blank."]})
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
        if not values["name"]:
            raise DomainError("validation_error", "Invalid input.", errors={"name": ["This field may not be blank."]})
    if "notes" in values:
        values["notes"] = values["notes"] or ""
    if "settings" in values and values["settings"] is None:
        values["settings"] = {}
    return values


def _save(action):
    try:
        with transaction.atomic():
            action()
    except IntegrityError as exc:
        raise (unique_conflict(exc, UNIQUE) or exc) from None


def _credential_name(agent: Agent) -> str:
    return f"Office agent {agent.code}"[:120]


# --------------------------------------------------------------------------------------------------------------------
# One-time config download
# --------------------------------------------------------------------------------------------------------------------
def _config_ttl() -> int:
    return int(setting("DEVICES_AGENT_CONFIG_TTL_SECONDS", 600))


def _config_key(download: str) -> str:
    return CONFIG_CACHE_PREFIX + hashlib.sha256(download.encode()).hexdigest()


def _offer_config(agent: Agent, token: str) -> dict:
    """Park the plaintext token (encrypted) for one download; returns ``{download, expires_at}``."""
    download = secrets.token_urlsafe(32)
    payload = encrypt_str(json.dumps({"agent_uid": str(agent.uid), "token": token}))
    ttl = _config_ttl()
    # Written at once: if the surrounding transaction rolls back, the parked token belongs to no credential and
    # verifies nowhere.
    cache.set(_config_key(download), payload, ttl)
    return {"download": download, "expires_at": now() + timedelta(seconds=ttl)}


def take_config_token(agent: Agent, download: str) -> str:
    """The token parked for ``download`` (single use); 410 ``download_expired`` when used, late or for another agent."""
    key = _config_key(download or "")
    stored = cache.get(key)
    if stored is None or not cache.delete(key):
        raise DomainError("download_expired", "This configuration download was already used or has expired; rotate the token to get a new one.", status=410)
    try:
        data = json.loads(decrypt_str(stored))
    except (DecryptionError, ValueError):
        raise DomainError("download_expired", "This configuration download can no longer be read; rotate the token to get a new one.", status=410) from None
    if data.get("agent_uid") != str(agent.uid):
        raise DomainError("download_expired", "This configuration download belongs to another agent.", status=410)
    return data["token"]


def render_agent_ini(agent: Agent, token: str, server_url: str) -> str:
    """``agent.ini`` for the office machine: the server, the credential, the queue and the central intervals."""
    return (
        "# Flarize office agent configuration — generated by the platform.\n"
        "# The token below is a secret: keep this file on the office machine only; rotating the token invalidates it.\n"
        "[agent]\n"
        f"server_url = {server_url.rstrip('/')}\n"
        f"api_version = {AGENT_API_VERSION}\n"
        f"token = {token}\n"
        f"agent_code = {agent.code}\n"
        "queue_path = agent_queue.sqlite3\n"
        f"heartbeat_interval = {agent.heartbeat_interval_seconds}\n"
        f"sync_interval = {agent.sync_interval_seconds}\n"
        "upload_batch_size = 200\n"
        "verify_tls = true\n"
        "\n"
        "# Terminals come from the central configuration (Studio → Devices). A [device:<n>] section here pins a terminal\n"
        "# locally (name, ip, port, expected_serial, expected_mac); the local file wins where the two disagree.\n"
    )


# --------------------------------------------------------------------------------------------------------------------
# CRUD and credential actions
# --------------------------------------------------------------------------------------------------------------------
@transaction.atomic
def create_agent(*, user, data) -> tuple[Agent, str, dict]:
    """Returns ``(agent, plaintext_token, config_download)``."""
    values = _normalise({name: data[name] for name in ("code", *EDITABLE_FIELDS) if name in data})
    _check_intervals(values)
    agent = Agent(**values)
    stamp_create(agent, user)
    _save(agent.save)
    credential, token = service_credentials.issue(ServiceCredential.Kind.AGENT, _credential_name(agent), agent, user=user)
    Agent.all_objects.filter(pk=agent.pk).update(credential=credential)
    agent.credential = credential
    record("devices.agent_created", obj=agent, actor=user, after=agent_snapshot(agent))
    bump_devices(NS_AGENTS)
    return agent, token, _offer_config(agent, token)


@transaction.atomic
def update_agent(instance: Agent, *, user, data, expected_version=None) -> Agent:
    agent = lock(Agent, instance, expected_version)
    before = agent_snapshot(agent)
    values = _normalise({name: data[name] for name in EDITABLE_FIELDS if name in data})
    values = {name: value for name, value in values.items() if getattr(agent, name) != value}
    if not values:
        return agent
    _check_intervals(values, agent)
    _save(lambda: agent.versioned_update(user, **values))
    changed_before, changed_after = changes(before, agent_snapshot(agent))
    record("devices.agent_updated", obj=agent, actor=user, before=changed_before, after=changed_after)
    bump_devices(NS_AGENTS)
    return agent


@transaction.atomic
def rotate_token(instance: Agent, *, user, expected_version=None) -> tuple[Agent, str, dict]:
    agent = lock(Agent, instance, expected_version, related=("credential",))
    credential = agent.credential
    if credential is not None and credential.revoked_at is None:
        credential, token = service_credentials.rotate(credential, user=user)
    else:
        credential, token = service_credentials.issue(ServiceCredential.Kind.AGENT, _credential_name(agent), agent, user=user)
    agent.versioned_update(user, credential=credential, is_active=True)
    record("devices.agent_token_rotated", obj=agent, actor=user, after={"prefix": credential.token_prefix})
    bump_devices(NS_AGENTS)
    return agent, token, _offer_config(agent, token)


@transaction.atomic
def revoke_agent(instance: Agent, *, user, expected_version=None) -> Agent:
    agent = lock(Agent, instance, expected_version, related=("credential",))
    if agent.credential is not None:
        service_credentials.revoke(agent.credential, user=user)
    if agent.is_active:
        agent.versioned_update(user, is_active=False)
    record("devices.agent_revoked", obj=agent, actor=user, after={"is_active": False})
    bump_devices(NS_AGENTS)
    return agents_queryset().get(pk=agent.pk)


@transaction.atomic
def delete_agent(instance: Agent, *, user, expected_version=None) -> None:
    agent = lock(Agent, instance, expected_version, related=("credential",))
    bound = Device.objects.filter(agent=agent).count()
    if bound:
        raise Conflict("agent_in_use", f"{agent.code} still carries {bound} device(s). Rehome them first, or revoke the agent.", errors={"devices": [str(bound)]})
    if agent.credential is not None:
        service_credentials.revoke(agent.credential, user=user)
    agent.soft_delete(user)
    record("devices.agent_deleted", obj=agent, actor=user, before=agent_snapshot(agent))
    bump_devices(NS_AGENTS)
