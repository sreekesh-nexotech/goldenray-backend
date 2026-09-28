"""``settings/integrations/`` — provider settings stored Fernet-encrypted in ``company_integration`` (PLAN §5.3).

Each key has a fixed schema (:data:`SCHEMAS`). The whole ``config`` document is one Fernet token at rest; the API
never returns a secret field — only whether it is set. ``PUT`` semantics per field:

* non-secret fields are replaced by the request (absent → the field's default, or empty when it has none);
* secret fields are **write-only**: absent → keep the stored value, a string → replace it, ``null``/``""`` → clear it;
* unknown fields are refused; an *enabled* integration must have every required field (stored or sent).

The platform reads enabled configurations through :func:`stored_config`, registered as the
:mod:`core.integrations` resolver in ``CompanyConfig.ready`` (Bunny uploads, SMTP delivery, Twilio Verify).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parseaddr

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator, validate_email
from django.db import IntegrityError, transaction

from audit.services import record
from company.models import Integration
from core.errors import DomainError, StaleVersion
from core.services import check_version, stamp_create

HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.IGNORECASE)
_URL = URLValidator(schemes=["https"])


class FieldError(ValueError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str = "str"  # str | int | bool | host | https_url | email_sender
    required: bool = True
    secret: bool = False
    default: object = None
    pattern: str | None = None
    max_length: int = 255
    min_length: int = 0
    min_value: int | None = None
    max_value: int | None = None

    def describe(self) -> dict:
        return {"name": self.name, "type": self.kind, "required": self.required, "secret": self.secret, "default": self.default}

    def clean(self, value):
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise FieldError("Must be true or false.")
            return value
        if self.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise FieldError("Must be an integer.")
            if (self.min_value is not None and value < self.min_value) or (self.max_value is not None and value > self.max_value):
                raise FieldError(f"Must be between {self.min_value} and {self.max_value}.")
            return value
        if not isinstance(value, str):
            raise FieldError("Must be a string.")
        value = value.strip()
        if len(value) > self.max_length or len(value) < self.min_length:
            raise FieldError(f"Must be {self.min_length}–{self.max_length} characters.")
        if self.pattern and value and not re.fullmatch(self.pattern, value):
            raise FieldError("Has an invalid format.")
        if self.kind == "host" and value and not HOST_RE.match(value):
            raise FieldError("Must be a host name.")
        if self.kind == "https_url" and value:
            try:
                _URL(value)
            except DjangoValidationError:
                raise FieldError("Must be an https:// URL.") from None
        if self.kind == "email_sender" and value:
            _, address = parseaddr(value)
            try:
                validate_email(address)
            except DjangoValidationError:
                raise FieldError("Must be an e-mail address, optionally with a display name.") from None
        return value


SCHEMAS: dict[str, tuple[FieldSpec, ...]] = {
    Integration.Key.TWILIO: (
        FieldSpec("account_sid", pattern=r"AC[0-9a-fA-F]{32}", max_length=34),
        FieldSpec("verify_service_sid", pattern=r"VA[0-9a-fA-F]{32}", max_length=34),
        FieldSpec("auth_token", secret=True, min_length=16, max_length=128),
    ),
    Integration.Key.BUNNY: (
        FieldSpec("storage_zone", pattern=r"[A-Za-z0-9-]{1,64}", max_length=64),
        # Only Bunny's own (regional) storage hosts: the access key is never sent anywhere else.
        FieldSpec("storage_endpoint", kind="host", required=False, default="storage.bunnycdn.com", pattern=r"([a-z]{2,4}\.)?storage\.bunnycdn\.com", max_length=64),
        FieldSpec("cdn_base_url", kind="https_url", max_length=200),
        FieldSpec("access_key", secret=True, min_length=20, max_length=128),
    ),
    Integration.Key.SMTP: (
        FieldSpec("host", kind="host"),
        FieldSpec("port", kind="int", required=False, default=587, min_value=1, max_value=65535),
        FieldSpec("username", required=False, default=""),
        FieldSpec("password", secret=True, required=False, max_length=256),
        FieldSpec("use_tls", kind="bool", required=False, default=True),
        FieldSpec("use_ssl", kind="bool", required=False, default=False),
        FieldSpec("from_email", kind="email_sender", required=False, default=""),
    ),
}


def _secret_names(key: str) -> list[str]:
    return [spec.name for spec in SCHEMAS[key] if spec.secret]


def _is_set(value) -> bool:
    return value not in (None, "")


def describe(key: str, row: Integration | None) -> dict:
    """API representation: non-secret values, which secrets are set, and the field schema."""
    stored = (row.config if row else {}) or {}
    config = {}
    for spec in SCHEMAS[key]:
        if not spec.secret:
            config[spec.name] = stored.get(spec.name, spec.default if spec.default is not None else "")
    return {
        "key": key,
        "label": Integration.Key(key).label,
        "uid": row.uid if row else None,
        "is_enabled": bool(row and row.is_enabled),
        "config": config,
        "secrets": {name: _is_set(stored.get(name)) for name in _secret_names(key)},
        "fields": [spec.describe() for spec in SCHEMAS[key]],
        "version": row.version if row else None,
        "updated_at": row.updated_at if row else None,
    }


def list_integrations() -> list[dict]:
    rows = {row.key: row for row in Integration.objects.all()}
    return [describe(key, rows.get(key)) for key in Integration.Key.values]


def _merge(key: str, incoming: dict, stored: dict, *, enabled: bool) -> tuple[dict, list[str], list[str]]:
    """``(new config, changed non-secret fields, changed secret fields)``; raises ``DomainError`` listing every problem."""
    if not isinstance(incoming, dict):
        raise DomainError("validation_error", "config must be an object.", errors={"config": ["Must be an object."]})
    specs = {spec.name: spec for spec in SCHEMAS[key]}
    errors: dict[str, list[str]] = {}
    unknown = sorted(set(incoming) - set(specs))
    if unknown:
        errors["config"] = [f"Unknown field(s): {', '.join(unknown)}."]
    merged: dict = {}
    for name, spec in specs.items():
        if spec.secret:
            if name not in incoming:
                value = stored.get(name)
            elif incoming[name] in (None, ""):
                value = None
            else:
                try:
                    value = spec.clean(incoming[name])
                except FieldError as exc:
                    errors[f"config.{name}"] = [str(exc)]
                    continue
        elif name in incoming and incoming[name] is not None:
            try:
                value = spec.clean(incoming[name])
            except FieldError as exc:
                errors[f"config.{name}"] = [str(exc)]
                continue
        else:
            value = spec.default if spec.default is not None else ""
        if _is_set(value):
            merged[name] = value
        if enabled and spec.required and not _is_set(value):
            errors.setdefault(f"config.{name}", []).append("Required while the integration is enabled.")
    if key == Integration.Key.SMTP and merged.get("use_tls") and merged.get("use_ssl"):
        errors["config.use_ssl"] = ["use_tls and use_ssl cannot both be on."]
    if errors:
        raise DomainError("validation_error", "The integration settings are invalid.", errors=errors)
    changed = sorted(name for name in specs if not specs[name].secret and merged.get(name) != stored.get(name))
    secrets_changed = sorted(name for name in specs if specs[name].secret and merged.get(name) != stored.get(name))
    return merged, changed, secrets_changed


@transaction.atomic
def put_integration(*, user, key: str, is_enabled: bool, config: dict, expected_version=None) -> dict:
    if key not in Integration.Key.values:
        raise DomainError("validation_error", "Unknown integration.", errors={"key": [f"Use one of {', '.join(Integration.Key.values)}."]})
    row = Integration.objects.select_for_update().filter(key=key).first()
    if row is not None:
        check_version(row, expected_version)
    stored = (row.config if row else {}) or {}
    merged, changed, secrets_changed = _merge(key, config, stored, enabled=is_enabled)
    was_enabled = bool(row and row.is_enabled)
    if row is None:
        row = Integration(key=key, config=merged, is_enabled=is_enabled)
        stamp_create(row, user)
        try:
            with transaction.atomic():
                row.save()
        except IntegrityError:
            raise StaleVersion() from None
    elif changed or secrets_changed or was_enabled != is_enabled:
        row.versioned_update(user, config=merged, is_enabled=is_enabled)
    else:
        return describe(key, row)
    record(
        "company.integration_updated",
        obj=row,
        actor=user,
        before={"is_enabled": was_enabled},
        after={"is_enabled": is_enabled, "fields_changed": changed, "write_only_fields_changed": secrets_changed, **{name: merged.get(name) for name in changed}},
    )
    return describe(key, row)


def stored_config(key: str) -> dict | None:
    """The :mod:`core.integrations` resolver: an enabled row's decrypted config, else ``None``."""
    row = Integration.objects.filter(key=key, is_enabled=True).first()
    return dict(row.config) if row and row.config else None
