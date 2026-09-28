"""Encryption at rest with MultiFernet — fail-closed.

``settings.FERNET_KEYS`` is an ordered list: the first key encrypts, every key decrypts (rotation: prepend a new
key, run :func:`reencrypt` over stored values, then drop the old key). When no key is configured every encrypt or
decrypt raises ``ImproperlyConfigured`` — there is no fallback key and plaintext is never stored or returned in
place of ciphertext (standard §5 "fail-open encryption" defect).
"""

from __future__ import annotations

import json
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class DecryptionError(Exception):
    """Stored ciphertext cannot be decrypted with any configured key."""


@lru_cache(maxsize=8)
def _build(keys: tuple[str, ...]) -> MultiFernet:
    try:
        return MultiFernet([Fernet(key.encode() if isinstance(key, str) else key) for key in keys])
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured("FERNET_KEYS contains an invalid Fernet key.") from exc


def get_fernet() -> MultiFernet:
    keys = tuple(key.strip() for key in (getattr(settings, "FERNET_KEYS", None) or ()) if key and key.strip())
    if not keys:
        raise ImproperlyConfigured("FERNET_KEYS is not configured; refusing to encrypt or decrypt (fail-closed).")
    return _build(keys)


def encrypt_str(plaintext: str) -> str:
    if not isinstance(plaintext, str):
        raise TypeError("encrypt_str() expects a str.")
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_str(token: str) -> str:
    fernet = get_fernet()
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, AttributeError) as exc:
        raise DecryptionError("Value cannot be decrypted with the configured FERNET_KEYS.") from exc


def reencrypt(token: str) -> str:
    """Re-encrypt ``token`` under the primary key (key rotation). Any value no key can decrypt → ``DecryptionError``."""
    fernet = get_fernet()
    try:
        return fernet.rotate(token.encode("ascii")).decode("ascii")
    except (InvalidToken, UnicodeError, AttributeError) as exc:
        raise DecryptionError("Value cannot be decrypted with the configured FERNET_KEYS.") from exc


class EncryptedTextField(models.TextField):
    """Text stored as a Fernet token. Not searchable or orderable by content (lookups other than isnull are refused)."""

    description = "Fernet-encrypted text"

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None:
            return None
        return encrypt_str(str(value))

    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        return decrypt_str(value)

    def get_lookup(self, lookup_name):
        if lookup_name != "isnull":
            raise TypeError(f"{self.__class__.__name__} does not support the '{lookup_name}' lookup (values are encrypted).")
        return super().get_lookup(lookup_name)


class EncryptedJSONField(models.TextField):
    """A JSON document stored as one Fernet token (e.g. ``company_integration.config``)."""

    description = "Fernet-encrypted JSON"

    def __init__(self, *args, encoder=DjangoJSONEncoder, **kwargs):
        self.encoder = encoder
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if self.encoder is not DjangoJSONEncoder:
            kwargs["encoder"] = self.encoder
        return name, path, args, kwargs

    def get_prep_value(self, value):
        if value is None:
            return None
        return encrypt_str(json.dumps(value, cls=self.encoder, sort_keys=True))

    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        return json.loads(decrypt_str(value))

    def to_python(self, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value

    def value_to_string(self, obj):
        return json.dumps(self.value_from_object(obj), cls=self.encoder)

    def get_lookup(self, lookup_name):
        if lookup_name != "isnull":
            raise TypeError(f"{self.__class__.__name__} does not support the '{lookup_name}' lookup (values are encrypted).")
        return super().get_lookup(lookup_name)
