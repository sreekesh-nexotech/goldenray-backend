"""``pricing/validity-policy/`` — how long an issued quotation stays valid (PLAN §2.3 ``pricing_validity_policy``).

One DEFAULT row per key (``QUOTATION``: ``days``, ``grace_days``, ``effective_from``) plus Flarize's effective windows
(``quotationPolicy.js`` Phase J): a new quotation resolves the ACTIVE window whose ``[effective_from, effective_to)``
contains its issue instant — the latest ``effective_from`` wins, ties by ``policy_id`` descending — and falls back to
the DEFAULT days. A PUT updates the default and, when ``windows`` is sent, upserts them by ``policy_id``; windows left
out become INACTIVE (never deleted, issued quotations may cite them). Every PUT bumps the default row's ``version``.
"""

from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.utils import timezone

from audit.services import record
from core.errors import Conflict, DomainError
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from pricing.models import ValidityKind, ValidityPolicy, ValidityWindowStatus
from pricing.services.common import AUTHORING_NAMESPACE, json_safe

DEFAULT_KEY = "QUOTATION"
WINDOW_FIELDS = ("days", "grace_days", "effective_from", "effective_to", "status", "version_label", "note")


def default_policy(key: str = DEFAULT_KEY) -> ValidityPolicy | None:
    return ValidityPolicy.objects.filter(key=key, kind=ValidityKind.DEFAULT).first()


def windows(key: str = DEFAULT_KEY):
    return ValidityPolicy.objects.filter(key=key, kind=ValidityKind.WINDOW).order_by("-effective_from", "-policy_id")


def resolve(at: datetime | None = None, key: str = DEFAULT_KEY) -> dict:
    """``resolveActivePolicy`` then the default: ``{days, grace_days, source, policy_id, version}``."""
    at = at or timezone.now()
    candidates = [window for window in windows(key).filter(status=ValidityWindowStatus.ACTIVE, effective_from__lte=at) if window.effective_to is None or window.effective_to > at]
    if candidates:
        candidates.sort(key=lambda window: (window.effective_from, window.policy_id), reverse=True)
        chosen = candidates[0]
        return {"days": chosen.days, "grace_days": chosen.grace_days, "source": "WINDOW", "policy_id": chosen.policy_id, "version": chosen.version_label}
    policy = default_policy(key)
    if policy is None:
        raise Conflict("policy_not_configured", f"No {key} validity policy is configured.")
    return {"days": policy.days, "grace_days": policy.grace_days, "source": "DEFAULT", "policy_id": "", "version": policy.version_label}


def _window_values(window: dict, index: int) -> dict:
    start, end = window.get("effective_from"), window.get("effective_to")
    if start is None:
        raise DomainError("validation_error", "A window needs effective_from.", errors={f"windows[{index}].effective_from": ["Required."]})
    if end is not None and end <= start:
        raise DomainError("validation_error", "A window must end after it starts.", errors={f"windows[{index}].effective_to": ["Must be after effective_from."]})
    return {name: window[name] for name in WINDOW_FIELDS if name in window}


@transaction.atomic
def put_policy(*, user, data: dict, expected_version=None, key: str = DEFAULT_KEY) -> ValidityPolicy:
    policy = ValidityPolicy.objects.select_for_update().filter(key=key, kind=ValidityKind.DEFAULT).first()
    values = {name: data[name] for name in ("days", "grace_days", "effective_from", "version_label", "note") if name in data}
    if policy is None:
        if expected_version not in (None, 1):
            raise Conflict("stale_version", "The policy does not exist yet; reload.")
        if "days" not in values:
            raise DomainError("validation_error", "The default validity needs days.", errors={"days": ["Required."]})
        values.setdefault("effective_from", timezone.now())
        policy = ValidityPolicy(key=key, kind=ValidityKind.DEFAULT, **values)
        stamp_create(policy, user)
        policy.save()
        before = None
    else:
        check_version(policy, expected_version)
        before = json_safe({name: getattr(policy, name) for name in ("days", "grace_days", "effective_from")})
        policy.versioned_update(user, **{name: value for name, value in values.items() if getattr(policy, name) != value})
    window_outcome = _replace_windows(key, data["windows"], user=user) if "windows" in data else None
    record(
        "pricing.validity_policy_set",
        obj=policy,
        actor=user,
        before=before,
        after=json_safe({**{name: getattr(policy, name) for name in ("days", "grace_days", "effective_from")}, "windows": window_outcome}),
    )
    bump(AUTHORING_NAMESPACE)
    return policy


def _replace_windows(key: str, items: list[dict], *, user) -> dict:
    outcome = {"created": 0, "updated": 0, "deactivated": 0}
    existing = {window.policy_id: window for window in windows(key).select_for_update()}
    seen = set()
    for index, item in enumerate(items):
        policy_id = (item.get("policy_id") or "").strip()
        if not policy_id:
            raise DomainError("validation_error", "A window needs a policy_id.", errors={f"windows[{index}].policy_id": ["Required."]})
        if policy_id in seen:
            raise DomainError("validation_error", "Duplicate policy_id.", errors={f"windows[{index}].policy_id": [policy_id]})
        seen.add(policy_id)
        values = _window_values(item, index)
        window = existing.get(policy_id)
        if window is None:
            window = ValidityPolicy(key=key, kind=ValidityKind.WINDOW, policy_id=policy_id, **values)
            stamp_create(window, user)
            window.save()
            outcome["created"] += 1
            continue
        diff = {name: value for name, value in values.items() if getattr(window, name) != value}
        if diff:
            window.versioned_update(user, **diff)
            outcome["updated"] += 1
    for policy_id, window in existing.items():
        if policy_id not in seen and window.status == ValidityWindowStatus.ACTIVE:
            window.versioned_update(user, status=ValidityWindowStatus.INACTIVE)
            outcome["deactivated"] += 1
    return outcome


def policy_payload(key: str = DEFAULT_KEY) -> dict | None:
    policy = default_policy(key)
    if policy is None:
        return None
    return json_safe(
        {
            "key": key,
            "days": policy.days,
            "grace_days": policy.grace_days,
            "effective_from": policy.effective_from,
            "windows": [
                {"policy_id": w.policy_id, "days": w.days, "grace_days": w.grace_days, "effective_from": w.effective_from, "effective_to": w.effective_to, "status": w.status}
                for w in windows(key).filter(status=ValidityWindowStatus.ACTIVE)
            ],
        }
    )
