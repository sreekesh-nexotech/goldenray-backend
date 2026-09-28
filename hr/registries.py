"""Extension points the later HR packages (``attendance``, ``devices``) fill. hr imports neither of them.

* :data:`office_summary` — named sections of ``GET hr/offices/<uid>/summary/?day=``; each provider receives
  ``(office, day, user)`` and returns a JSON object, or ``None`` to leave its section out (e.g. the user may not see
  attendance). Attendance adds day counts, devices adds agents/devices/raw punches.
* :data:`employee_dependencies` — named counters of ``GET hr/employees/<uid>/dependencies/`` and of the delete
  guard (an employee with any history cannot be deleted, only deactivated); providers receive the employee and
  return ``{counter: int}`` (attendance days, raw punches, device mappings).
* :data:`office_dependencies` — counters that block deleting an office (devices, agents) — ``{counter: int}``.
* :data:`device_mappings` — the single provider of ``GET hr/employees/<uid>/device-mappings/`` (devices package):
  ``fn(employee) -> {"mappings": [...], ...}``.
* :data:`device_reconciler` — the single provider of ``POST hr/employees/reconcile-devices/``:
  ``fn(user=, read_devices=, apply=) -> dict``.

A provider that raises is logged and skipped (a broken section never breaks the HR screens), except the reconciler,
whose errors reach the caller.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger("flarize.hr")


class Sections:
    """Named providers, called in registration order."""

    def __init__(self, label: str):
        self.label = label
        self._providers: dict[str, Callable] = {}

    def register(self, name: str):
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"Invalid {self.label} name {name!r}.")

        def decorator(fn: Callable) -> Callable:
            self._providers[name] = fn
            return fn

        return decorator

    def unregister(self, name: str) -> None:
        self._providers.pop(name, None)

    def names(self) -> list[str]:
        return list(self._providers)

    def collect(self, *args, **kwargs) -> dict:
        result = {}
        for name, fn in list(self._providers.items()):
            try:
                value = fn(*args, **kwargs)
            except Exception:  # noqa: BLE001 - one broken provider must not break the HR screen
                logger.exception("hr registry provider failed", extra={"registry": self.label, "provider": name})
                continue
            if value is not None:
                result[name] = value
        return result


class Provider:
    """At most one provider (the devices package)."""

    def __init__(self, label: str):
        self.label = label
        self._fn: Callable | None = None

    def set(self, fn: Callable | None) -> Callable | None:
        self._fn = fn
        return fn

    @property
    def available(self) -> bool:
        return self._fn is not None

    def __call__(self, *args, **kwargs):
        if self._fn is None:
            raise LookupError(f"No {self.label} provider is installed.")
        return self._fn(*args, **kwargs)


office_summary = Sections("office summary section")
employee_dependencies = Sections("employee dependency counter")
office_dependencies = Sections("office dependency counter")
device_mappings = Provider("device mappings")
device_reconciler = Provider("device reconciliation")


def counters(sections: Sections, *args) -> dict[str, int]:
    """Flatten ``{provider: {counter: n}}`` into ``{counter: n}`` (integers only)."""
    flat: dict[str, int] = {}
    for values in sections.collect(*args).values():
        for key, value in dict(values).items():
            flat[str(key)] = int(value)
    return flat
