"""Dashboard counters registry behind ``GET /api/<version>/dashboard/``.

Each app registers counters for its registry module; the dashboard returns only modules the user can ``view``,
and counter functions receive the user so they apply record scope themselves (``core.scopes.apply``)::

    @dashboard.register("leads")
    def lead_counts(user) -> dict[str, int]:
        qs = scopes.apply(Lead.objects.all(), user, "leads")
        return {"open": qs.filter(status="NEW").count()}

A counter registered with ``flag="<FEATURE_FLAG>"`` runs only while that flag is on; a module whose every counter is
switched off by its flag is left out entirely (a disabled feature is indistinguishable from an absent one).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger("flarize.dashboard")

Counter = Callable[[object], dict[str, int]]
_COUNTERS: dict[str, list[Counter]] = {}
_COUNTER_FLAGS: dict[Counter, str] = {}


def register(module: str, *, flag: str | None = None):
    from accounts.registry import MODULES

    if module not in MODULES:
        raise ValueError(f"Unknown registry module {module!r}.")

    def decorator(fn: Counter) -> Counter:
        existing = _COUNTERS.setdefault(module, [])
        if fn not in existing:
            existing.append(fn)
        if flag:
            _COUNTER_FLAGS[fn] = flag
        else:
            _COUNTER_FLAGS.pop(fn, None)
        return fn

    return decorator


def unregister(module: str, fn: Counter) -> None:
    if fn in _COUNTERS.get(module, []):
        _COUNTERS[module].remove(fn)
    _COUNTER_FLAGS.pop(fn, None)


def _switched_on(fn: Counter) -> bool:
    flag = _COUNTER_FLAGS.get(fn)
    if flag is None:
        return True
    from core.flags import flag_enabled

    return flag_enabled(flag)


def counters_for(user) -> dict[str, dict[str, int]]:
    """``{module: {counter: value}}`` for every module the user may view. A failing counter is skipped (logged)."""
    from accounts.services.authz import can

    result: dict[str, dict[str, int]] = {}
    for module in sorted(_COUNTERS):
        if not can(user, module, "view"):
            continue
        active = [fn for fn in _COUNTERS[module] if _switched_on(fn)]
        if _COUNTERS[module] and not active:
            continue
        values: dict[str, int] = {}
        for fn in active:
            try:
                values.update({str(key): int(value) for key, value in fn(user).items()})
            except Exception:  # noqa: BLE001 - one broken counter must not break the dashboard
                logger.exception("dashboard counter failed", extra={"registry_module": module})
        result[module] = values
    return result
