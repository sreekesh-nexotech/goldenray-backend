"""Panel-to-device allocator — port of Flarize ``src/lib/deviceAllocation.js`` (locked 2.10).

Given a panel count and candidate per-panel devices (micro-inverters, optimisers) each declaring
``panelsPerDevice``, find how many of each device cover EVERY panel exactly. When no exact cover exists the result is
BLOCKED with no partial allocation (the reason shows a greedy partial for context only).

Deterministic preference among exact covers: fewest devices, then the higher "largest bias" (larger devices first).
Pure: no catalog, no I/O; the caller supplies candidates.
"""

from __future__ import annotations

from typing import Any

from engines._jscompat import UNDEFINED, is_array, is_integer, js_floor, js_join, js_number, js_str, truthy

ALLOCATION_CODE = {
    "VALID": "VALID",
    "NOT_APPLICABLE": "NOT_APPLICABLE",
    "BLOCKED_NO_CANDIDATES": "BLOCKED_NO_CANDIDATES",
    "BLOCKED_NO_USABLE_CANDIDATES": "BLOCKED_NO_USABLE_CANDIDATES",
    "BLOCKED_INCOMPLETE_ALLOCATION": "BLOCKED_INCOMPLETE_ALLOCATION",
    "BLOCKED_PANELS_INVALID": "BLOCKED_PANELS_INVALID",
}


def _get(obj: Any, key: str) -> Any:
    return obj.get(key, UNDEFINED) if isinstance(obj, dict) else UNDEFINED


def _candidate_problem(candidate: Any) -> str | None:
    if not isinstance(candidate, (dict, list, tuple)):
        return "not an object"
    component_id = _get(candidate, "componentId")
    if not isinstance(component_id, str) or not component_id:
        return "missing componentId"
    device_type = _get(candidate, "deviceType")
    if not isinstance(device_type, str) or not device_type:
        return "missing deviceType"
    if device_type == "string_inverter":
        return "string_inverter is not a per-panel device"
    n = js_number(_get(candidate, "panelsPerDevice"))
    if not is_integer(n) or n <= 0:
        return f"panelsPerDevice={js_str(_get(candidate, 'panelsPerDevice'))} is not a positive integer"
    return None


def _ppd(candidate: dict) -> int | float:
    return js_number(candidate["panelsPerDevice"])


def _find_exact_allocation(panels: int, usable: list[dict]) -> list[dict] | None:
    if panels == 0:
        return []
    ordered = sorted(usable, key=lambda c: -_ppd(c))  # stable, largest panelsPerDevice first
    size = len(ordered)
    dp: list[dict | None] = [None] * (panels + 1)
    dp[0] = {"qty": [0] * size, "totalUnits": 0, "largestBias": 0}
    for k in range(1, panels + 1):
        best = None
        for i, candidate in enumerate(ordered):
            cap = int(_ppd(candidate))
            if cap > k:
                continue
            prev = dp[k - cap]
            if prev is None:
                continue
            qty = list(prev["qty"])
            qty[i] += 1
            option = {"qty": qty, "totalUnits": prev["totalUnits"] + 1, "largestBias": prev["largestBias"] + (size - i)}
            if best is None or option["totalUnits"] < best["totalUnits"] or (option["totalUnits"] == best["totalUnits"] and option["largestBias"] > best["largestBias"]):
                best = option
        dp[k] = best
    solution = dp[panels]
    if solution is None:
        return None
    lines = []
    for i, candidate in enumerate(ordered):
        quantity = solution["qty"][i]
        if quantity > 0:
            lines.append(
                {
                    "componentId": candidate["componentId"],
                    "deviceType": candidate["deviceType"],
                    "panelsPerDevice": candidate["panelsPerDevice"],
                    "quantity": quantity,
                    "coveredPanels": quantity * _ppd(candidate),
                    "name": candidate.get("name") if truthy(candidate.get("name")) else None,
                    "price": candidate.get("price") if candidate.get("price") is not None else None,
                }
            )
    return lines


def allocate_devices(*, panels: Any = UNDEFINED, candidates: Any = UNDEFINED) -> dict:
    """``allocateDevices({panels, candidates})`` → ``{status, code, panels, allocations, unallocatedPanels, reason, skipped}``."""
    n = js_number(panels)
    if not is_integer(n) or n < 0:
        return {
            "status": "BLOCKED",
            "code": ALLOCATION_CODE["BLOCKED_PANELS_INVALID"],
            "panels": n,
            "allocations": [],
            "unallocatedPanels": n,
            "reason": f"panels={js_str(panels)} is not a non-negative integer",
            "skipped": [],
        }
    n = int(n)
    if n == 0:
        return {
            "status": "NOT_APPLICABLE",
            "code": ALLOCATION_CODE["NOT_APPLICABLE"],
            "panels": 0,
            "allocations": [],
            "unallocatedPanels": 0,
            "reason": "no panels to allocate",
            "skipped": [],
        }
    items = list(candidates) if is_array(candidates) else []
    if not items:
        return {
            "status": "BLOCKED",
            "code": ALLOCATION_CODE["BLOCKED_NO_CANDIDATES"],
            "panels": n,
            "allocations": [],
            "unallocatedPanels": n,
            "reason": "no device candidates supplied",
            "skipped": [],
        }
    usable, skipped = [], []
    for candidate in items:
        problem = _candidate_problem(candidate)
        if problem:
            skipped.append({"candidate": candidate, "reason": problem})
        else:
            usable.append(candidate)
    if not usable:
        return {
            "status": "BLOCKED",
            "code": ALLOCATION_CODE["BLOCKED_NO_USABLE_CANDIDATES"],
            "panels": n,
            "allocations": [],
            "unallocatedPanels": n,
            "reason": f"no usable device candidates ({len(skipped)} skipped)",
            "skipped": skipped,
        }
    allocation = _find_exact_allocation(n, usable)
    if allocation is None:
        remaining: int | float = n
        partial = []
        for candidate in sorted(usable, key=lambda c: -_ppd(c)):
            quantity = js_floor(remaining / _ppd(candidate))
            if quantity > 0:
                partial.append(f"{js_str(quantity)}× {candidate['componentId']}(cap={js_str(candidate['panelsPerDevice'])})")
                remaining -= quantity * _ppd(candidate)
        caps = js_join([c["panelsPerDevice"] for c in usable], ",")
        uncovered = f"; {js_str(remaining)} panel(s) uncovered" if remaining > 0 else ""
        return {
            "status": "BLOCKED",
            "code": ALLOCATION_CODE["BLOCKED_INCOMPLETE_ALLOCATION"],
            "panels": n,
            "allocations": [],
            "unallocatedPanels": remaining if remaining > 0 else 0,
            "reason": (
                f"no exact-coverage allocation for {n} panels across candidates (caps: {caps}). "
                f"Best partial: {' + '.join(partial) or 'none'}{uncovered}. "
                "Add a device candidate that covers the remainder or change panel count."
            ),
            "skipped": skipped,
        }
    return {
        "status": "VALID",
        "code": ALLOCATION_CODE["VALID"],
        "panels": n,
        "allocations": allocation,
        "unallocatedPanels": 0,
        "reason": None,
        "skipped": skipped,
    }


__all__ = ["ALLOCATION_CODE", "allocate_devices"]
