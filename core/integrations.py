"""Provider configuration for the external integrations (Twilio Verify, Bunny, SMTP) — dependency inversion.

Platform code (``media``'s Bunny client, ``core.notifications``) needs provider settings, but the Admin-editable,
Fernet-encrypted values live in ``company_integration`` — the ``company`` app, which sits *above* the platform and
must never be imported by it (``.importlinter`` "platform-is-the-bottom"). So the company app registers one resolver
at startup (``CompanyConfig.ready``) and platform code asks::

    config = integrations.get_config("BUNNY")   # dict of the enabled row's values, or None
    if config is None:
        ...fall back to environment settings...

Nothing is cached: one indexed read per call (uploads, e-mails and OTPs are rare), so an Admin edit applies to the
next call in every process and decrypted secrets never sit in Redis or in a long-lived process cache.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

TWILIO = "TWILIO"
BUNNY = "BUNNY"
SMTP = "SMTP"
KEYS: tuple[str, ...] = (TWILIO, BUNNY, SMTP)

Resolver = Callable[[str], Mapping | None]
_resolver: Resolver | None = None


def register_resolver(fn: Resolver) -> Resolver:
    """Install the function that returns an *enabled* integration's config (or ``None``). One resolver only."""
    global _resolver
    _resolver = fn
    return fn


def unregister_resolver() -> None:
    global _resolver
    _resolver = None


def get_config(key: str) -> dict | None:
    """The enabled stored configuration for ``key`` (secrets decrypted), or ``None`` when none is stored/enabled.

    Errors (database, undecryptable values) propagate: encryption is fail-closed, so a caller never silently
    continues with half a configuration.
    """
    if key not in KEYS:
        raise ValueError(f"Unknown integration {key!r}; expected one of {', '.join(KEYS)}.")
    if _resolver is None:
        return None
    config = _resolver(key)
    return dict(config) if config else None
