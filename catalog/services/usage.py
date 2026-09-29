"""Where a component is used — the registry behind ``GET catalog/components/<uid>/usage/`` and the delete guard.

Other contexts register a provider once (from their ``AppConfig.ready()``)::

    from catalog.services import usage

    @usage.register("packs.config_lines")
    def pack_lines(component):
        return [{"object_type": "packs.configpack", "object_uid": line.pack.uid, "label": str(line.pack)} for line in …]

A provider receives the component and returns its **live** references as dicts with ``object_type``,
``object_uid`` and optionally ``label``/``status``. Catalog never imports the consumers (arrows point down).
A component with any reference cannot be deleted (409 ``component_in_use``); a provider that fails is reported
(``error``) and blocks deletion too — the guard fails closed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from catalog.models import Component
from core.errors import Conflict

logger = logging.getLogger("flarize.catalog.usage")

Provider = Callable[[Component], Iterable[dict]]
_PROVIDERS: dict[str, Provider] = {}
MAX_REFERENCES = 100


@dataclass
class UsageSection:
    name: str
    count: int = 0
    references: list[dict] = field(default_factory=list)
    error: bool = False


def register(name: str):
    """Register the usage provider ``name`` (``<context>.<what>``). Re-registering a name replaces it."""
    if not name or "." not in name:
        raise ValueError("Usage provider names are '<context>.<what>'.")

    def decorator(fn: Provider) -> Provider:
        _PROVIDERS[name] = fn
        return fn

    return decorator


def unregister(name: str) -> None:
    _PROVIDERS.pop(name, None)


def registered() -> list[str]:
    return sorted(_PROVIDERS)


def _reference(item: dict) -> dict:
    return {
        "object_type": str(item.get("object_type", "")),
        "object_uid": str(item["object_uid"]) if item.get("object_uid") is not None else None,
        "label": str(item.get("label", "")),
        "status": str(item.get("status", "")),
    }


def usage_of(component: Component) -> list[UsageSection]:
    sections = []
    for name in registered():
        section = UsageSection(name=name)
        try:
            references = [_reference(item) for item in _PROVIDERS[name](component) or ()]
        except Exception:  # noqa: BLE001 - one broken provider must not hide the others; the guard fails closed
            logger.exception("catalog usage provider failed", extra={"provider": name})
            section.error = True
        else:
            section.count = len(references)
            section.references = references[:MAX_REFERENCES]
        sections.append(section)
    return sections


def ensure_not_in_use(component: Component) -> None:
    sections = usage_of(component)
    used = {section.name: [f"{section.count} references"] for section in sections if section.count}
    failed = {section.name: ["Could not be checked."] for section in sections if section.error}
    if used or failed:
        raise Conflict("component_in_use", "The component is still referenced; retire it instead of deleting it.", errors={**used, **failed})


@register("catalog.replacements")
def replaced_components(component: Component):
    """Live components that name this one as their replacement."""
    for other in Component.objects.filter(replacement=component).only("uid", "sku", "name", "status"):
        yield {"object_type": "catalog.component", "object_uid": other.uid, "label": f"{other.sku} — {other.name}", "status": other.status}
