"""Password hashers beyond Django's own.

``LegacyBCryptPasswordHasher`` verifies the bcrypt hashes imported from the eSSL attendance app (PLAN §7.5: stored as
``bcrypt$<hash>`` and upgraded to Argon2 on the next successful login). eSSL truncated passwords to 72 bytes before
hashing and verifying (bcrypt only reads 72 bytes); ``bcrypt`` ≥ 5 raises on longer input instead, which made a
long password a 500 on login. The same truncation keeps those people's passwords working and never raises.
Algorithm name and format are Django's ``bcrypt`` — nothing already stored changes.
"""

from __future__ import annotations

from django.contrib.auth.hashers import BCryptPasswordHasher
from django.utils.encoding import force_bytes

BCRYPT_MAX_BYTES = 72


class LegacyBCryptPasswordHasher(BCryptPasswordHasher):
    def encode(self, password, salt):
        return super().encode(force_bytes(password)[:BCRYPT_MAX_BYTES], salt)
