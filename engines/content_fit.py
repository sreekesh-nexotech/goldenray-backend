"""Quotation content and branding: the bilingual fit guard, language helpers, appliance rows, content-store lifecycle,
and the versioned branding freeze.

Ports of ``quotationContent.js`` (P1, 2026-09-13; workflows spec B.12, C.12, F.2) and ``quotationBranding.js``
(Phases F + I; workflows spec H.2). ``quotations_content_version.fit_report`` is :func:`fit_summary`; a published
content version is frozen into ``payload.content`` at issue; ``snapshot.branding`` is :func:`freeze_branding_snapshot`.

**Fit guard.** Every text field is ``{en, ml}`` with separate per-language limits derived from the A4 layout; lengths
are UTF-16 code units (``String.length`` in the browser that counts in the editor), so a character outside the BMP
counts twice. :func:`validate_content` returns every violation ``{path, code, message[, length, max | total, cap]}``;
a draft with any violation cannot be saved or published (:class:`ContentInvalid`, ``CONTENT_INVALID``).

**Store lifecycle** functions are pure: they return a new store instead of mutating the one given, and take ``at``
from the caller (the JavaScript defaulted to the clock). **Branding** write functions keep the data rules (immutable
versions, accounts never deleted, one primary) but not the legacy ``ADMIN``/``PROJECT_HEAD`` role check —
authorisation is the platform registry's job, before the engine is called.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, localcontext
from typing import Any

from engines.frozen import FrozenDict, deep_freeze, thaw, to_json
from engines.jscompat import EXACT, UNDEFINED, coalesce, is_nullish, js_array, js_keys, js_number, js_string, js_trim, js_truthy, number_text, prop, round_places, utf16_len

__all__ = [
    "CONTENT_STORE_VERSION",
    "LANGUAGES",
    "DEFAULT_LANGUAGE",
    "LIMITS",
    "FALLBACK_DAILY_GEN_PER_KW",
    "pick",
    "normalise_language",
    "label",
    "profile_key_for_kw",
    "materialise_rows",
    "total_units",
    "default_rows_for_kw",
    "daily_generation_for_kw",
    "validate_content",
    "fit_summary",
    "ContentInvalid",
    "create_empty_store",
    "get_published",
    "get_draft",
    "save_draft",
    "publish_draft",
    "discard_draft",
    "describe_store",
    "BRANDING_KINDS",
    "MULTI_ACCOUNT_KINDS",
    "BRANDING_SCHEMA_V1",
    "BRANDING_SCHEMA_V2",
    "BrandingError",
    "current_version_id",
    "get_version",
    "freeze_branding_snapshot",
    "list_accounts",
    "kind_contains_demo",
    "publish_branding_version",
    "set_branding_account_status",
    "set_primary_branding_account",
    "describe_branding_store",
    "demo_branding_kinds",
]

CONTENT_STORE_VERSION = "quotationContent.1"
LANGUAGES = ("en", "ml")
DEFAULT_LANGUAGE = "en"

#: Per-field limits (UTF-16 code units; Malayalam gets its own, larger budgets).
LIMITS: Mapping[str, Any] = deep_freeze(
    {
        "terms": {"min": 1, "max": 19, "title": {"en": 60, "ml": 90}, "body": {"en": 1000, "ml": 1100}, "totalBody": {"en": 9200, "ml": 9800}},
        "timeline": {"min": 4, "max": 6, "days": {"en": 6, "ml": 6}, "title": {"en": 42, "ml": 60}, "body": {"en": 170, "ml": 190}},
        "weHandle": {"min": 3, "max": 8, "item": {"en": 120, "ml": 140}},
        "customerScope": {"min": 3, "max": 8, "item": {"en": 270, "ml": 280}},
        "requiredDocuments": {"min": 1, "max": 8, "item": {"en": 60, "ml": 90}},
        "lifeNow": {"min": 3, "max": 3, "title": {"en": 32, "ml": 48}, "body": {"en": 90, "ml": 110}},
        "lifeSolar": {"min": 3, "max": 3, "title": {"en": 32, "ml": 48}, "body": {"en": 90, "ml": 110}},
        "extraStructureCost": {"label": {"en": 48, "ml": 64}, "value": 20},
        "labels": {"en": 300, "ml": 360},
        "appliances": {"masterMax": 16, "profileRowsMax": 9, "nameMax": 28, "wattsMax": 10000, "hoursMax": 24, "qtyMax": 50, "loadRatioMax": Decimal("0.95")},
    }
)

#: Daily generation per kW used by the profile fit rule when the caller gives none (energy-config is the authority).
FALLBACK_DAILY_GEN_PER_KW = Decimal("4.0")

_APPLIANCE_ID = re.compile(r"[a-z0-9_]{2,32}", re.ASCII)


def _is_obj(value: Any) -> bool:
    return isinstance(value, Mapping)


def _str(value: Any) -> str:
    """``typeof v === 'string' ? v : v == null ? '' : String(v)``."""
    if isinstance(value, str):
        return value
    return "" if is_nullish(value) else js_string(value)


# ---------------------------------------------------------------------------------------------------------------------
# Language helpers
# ---------------------------------------------------------------------------------------------------------------------


def pick(field: Any, lang: Any = DEFAULT_LANGUAGE) -> str:
    """The text for a language, falling back to English, then to any language."""
    if is_nullish(field):
        return ""
    if isinstance(field, str):
        return field
    if not _is_obj(field):
        return ""
    value = field.get(lang) if isinstance(lang, str) else None
    if isinstance(value, str) and js_trim(value):
        return value
    english = field.get("en")
    if isinstance(english, str) and js_trim(english):
        return english
    for key in js_keys(field):
        if isinstance(field[key], str) and js_trim(field[key]):
            return field[key]
    return ""


def normalise_language(value: Any) -> str:
    """``'ML'``, ``'malayalam'``, ``'ml-IN'`` → ``'ml'``; anything else → ``'en'``."""
    text = js_trim(_str(value)).lower()
    return "ml" if text in ("ml", "malayalam", "ml-in") else "en"


_PLACEHOLDER = re.compile(r"\{(\w+)\}", re.ASCII)


def label(content: Any, key: str, lang: Any = DEFAULT_LANGUAGE, variables: Mapping | None = None) -> str:
    """A content label with ``{kw}``-style interpolation (a missing variable prints as nothing)."""
    raw = pick(prop(prop(content, "labels"), key), lang)
    if not variables:
        return raw
    return _PLACEHOLDER.sub(lambda match: "" if is_nullish(variables.get(match.group(1), UNDEFINED)) else js_string(variables[match.group(1)]), raw)


# ---------------------------------------------------------------------------------------------------------------------
# Appliances (page 3)
# ---------------------------------------------------------------------------------------------------------------------


def _numeric_keys(profiles: Any) -> list[Decimal]:
    keys = [js_number(key) for key in js_keys(profiles)]
    return sorted(key for key in keys if key.is_finite())


def profile_key_for_kw(profiles: Any, kw: Any) -> str | None:
    """The profile for a system size: the largest configured band ≤ kW, else the smallest."""
    keys = _numeric_keys(profiles)
    if not keys:
        return None
    size = js_number(kw)
    best = None
    for key in keys:
        if not size.is_nan() and key <= size:
            best = key
    return number_text(keys[0] if best is None else best)


def _non_negative(value: Any) -> Decimal:
    """``Math.max(0, Number(v) || 0)``."""
    number = js_number(value)
    if number.is_nan() or number == 0:
        return Decimal(0)
    return max(Decimal(0), number)


def _master(content: Any) -> dict:
    out: dict = {}
    for entry in js_array(prop(prop(content, "appliances"), "master")) or []:
        identifier = prop(entry, "id")
        try:
            out[identifier] = entry
        except TypeError:
            continue
    return out


def materialise_rows(content: Any, rows: Any, lang: Any = DEFAULT_LANGUAGE) -> tuple:
    """Profile rows ``[{id, qty, hours, watts?}]`` → printable rows with units/day (watts and name from the master)."""
    master = _master(content)
    out = []
    for row in js_array(rows) or []:
        try:
            entry = master.get(prop(row, "id"))
        except TypeError:
            entry = None
        if entry is None:
            continue
        qty = _non_negative(prop(row, "qty"))
        hours = _non_negative(coalesce(prop(row, "hours"), prop(entry, "defaultHours")))
        watts = _non_negative(coalesce(prop(row, "watts"), prop(entry, "watts")))
        with localcontext(EXACT):
            units = round_places(qty * watts * hours / 1000, 1)
        icon = prop(entry, "icon")
        out.append({"id": prop(entry, "id"), "name": pick(prop(entry, "name"), lang), "icon": icon if js_truthy(icon) else "", "qty": qty, "hours": hours, "watts": watts, "units": units})
    return deep_freeze(out)


def total_units(rows: Any) -> Decimal:
    """``round1(Σ units)``."""
    total = Decimal(0)
    with localcontext(EXACT):
        for row in js_array(rows) or []:
            number = js_number(prop(row, "units"))
            total += number if number.is_finite() else 0
    return round_places(total, 1)


def default_rows_for_kw(content: Any, kw: Any) -> Mapping:
    """The published profile for a kW (qty/hours only; watts come from the master when printed)."""
    profiles = prop(prop(content, "appliances"), "profiles")
    key = profile_key_for_kw(profiles, kw)
    rows = js_array(prop(profiles, key)) if key is not None else []
    return deep_freeze({"profileKey": key, "rows": [{"id": prop(row, "id"), "qty": prop(row, "qty"), "hours": prop(row, "hours")} for row in rows or []]})


def daily_generation_for_kw(kw: Any, daily_gen_per_kw: Any = FALLBACK_DAILY_GEN_PER_KW) -> Decimal:
    """``round1(kW × dailyGenPerKw)`` (the fallback applies to a missing or zero rate)."""
    rate = js_number(daily_gen_per_kw if js_truthy(daily_gen_per_kw) else FALLBACK_DAILY_GEN_PER_KW)
    with localcontext(EXACT):
        return round_places(js_number(kw) * rate, 1)


# ---------------------------------------------------------------------------------------------------------------------
# Fit guard
# ---------------------------------------------------------------------------------------------------------------------


def _check_text(errors: list, path: str, field: Any, limits: Mapping, required: bool = True) -> None:
    for lang in LANGUAGES:
        text = js_trim(_str(prop(field, lang)))
        if not text and required:
            errors.append({"path": f"{path}.{lang}", "code": "REQUIRED", "message": f"{path} ({lang}) is required"})
            continue
        maximum = limits[lang]
        length = utf16_len(text)
        if length > maximum:
            errors.append(
                {
                    "path": f"{path}.{lang}",
                    "code": "TOO_LONG",
                    "message": f"{path} ({lang}) is {length - maximum} characters over the limit of {maximum}",
                    "length": length,
                    "max": maximum,
                }
            )


def _check_list(errors: list, path: str, items: Any, limits: Mapping, item_check: Any) -> None:
    if not isinstance(items, (list, tuple)):
        errors.append({"path": path, "code": "INVALID", "message": f"{path} must be a list"})
        return
    if len(items) < limits["min"]:
        errors.append({"path": path, "code": "TOO_FEW", "message": f"{path} needs at least {limits['min']} item(s)"})
    if len(items) > limits["max"]:
        errors.append({"path": path, "code": "TOO_MANY", "message": f"{path} allows at most {limits['max']} item(s) — the page cannot fit more"})
    for index, item in enumerate(items[: limits["max"]]):
        item_check(f"{path}[{index + 1}]", item)


def _number_between(value: Any, low: Any, high: Any, *, strict_low: bool = False) -> bool:
    number = js_number(value)
    if number.is_nan():
        return False
    return (number > low if strict_low else number >= low) and number <= high


def validate_content(content: Any, daily_gen_per_kw: Any = None) -> tuple:
    """``validateContent``: every fit violation (empty when the content fits). ``daily_gen_per_kw`` tightens the
    appliance-profile rule to the live energy configuration."""
    if not _is_obj(content):
        return deep_freeze([{"path": "", "code": "INVALID", "message": "content must be an object"}])
    errors: list[dict[str, Any]] = []
    limits = LIMITS

    labels = content.get("labels", UNDEFINED)
    if not _is_obj(labels):
        errors.append({"path": "labels", "code": "INVALID", "message": "labels missing"})
    else:
        for key in js_keys(labels):
            _check_text(errors, f"labels.{key}", labels[key], limits["labels"])

    def two(section: str, *fields: str):
        def check(path: str, item: Any) -> None:
            for name in fields:
                _check_text(errors, f"{path}.{name}", prop(item, name), limits[section][name])

        return check

    def one(section: str):
        return lambda path, item: _check_text(errors, path, item, limits[section]["item"])

    terms = content.get("terms", UNDEFINED)
    _check_list(errors, "terms", terms, limits["terms"], two("terms", "title", "body"))
    if isinstance(terms, (list, tuple)):
        for lang in LANGUAGES:
            total = sum(utf16_len(_str(prop(prop(term, "body"), lang))) + utf16_len(_str(prop(prop(term, "title"), lang))) for term in terms)
            maximum = limits["terms"]["totalBody"][lang]
            if total > maximum:
                errors.append({"path": f"terms.{lang}", "code": "TOO_LONG", "message": f"Terms ({lang}) total {total} characters — pages 10–11 fit {maximum}", "length": total, "max": maximum})
    _check_list(errors, "timeline", content.get("timeline", UNDEFINED), limits["timeline"], two("timeline", "days", "title", "body"))
    _check_list(errors, "weHandle", content.get("weHandle", UNDEFINED), limits["weHandle"], one("weHandle"))
    _check_list(errors, "customerScope", content.get("customerScope", UNDEFINED), limits["customerScope"], one("customerScope"))
    _check_list(errors, "requiredDocuments", content.get("requiredDocuments", UNDEFINED), limits["requiredDocuments"], one("requiredDocuments"))
    _check_list(errors, "lifeNow", content.get("lifeNow", UNDEFINED), limits["lifeNow"], two("lifeNow", "title", "body"))
    _check_list(errors, "lifeSolar", content.get("lifeSolar", UNDEFINED), limits["lifeSolar"], two("lifeSolar", "title", "body"))
    extra = content.get("extraStructureCost", UNDEFINED)
    if _is_obj(extra):
        _check_text(errors, "extraStructureCost.label", extra.get("label", UNDEFINED), limits["extraStructureCost"]["label"])
        if utf16_len(_str(extra.get("value", UNDEFINED))) > limits["extraStructureCost"]["value"]:
            errors.append({"path": "extraStructureCost.value", "code": "TOO_LONG", "message": f"extraStructureCost.value over {limits['extraStructureCost']['value']} characters"})

    appliances = content.get("appliances", UNDEFINED)
    alimits = limits["appliances"]
    if not _is_obj(appliances) or not isinstance(appliances.get("master"), (list, tuple)) or not _is_obj(appliances.get("profiles")):
        errors.append({"path": "appliances", "code": "INVALID", "message": "appliances.master (list) and appliances.profiles (object) are required"})
        return deep_freeze(errors)
    master = appliances["master"]
    if len(master) > alimits["masterMax"]:
        errors.append({"path": "appliances.master", "code": "TOO_MANY", "message": f"at most {alimits['masterMax']} appliances"})
    ids: list = []
    for index, entry in enumerate(master):
        path = f"appliances.master[{index + 1}]"
        identifier = prop(entry, "id")
        if not _APPLIANCE_ID.fullmatch(_str(identifier)):
            errors.append({"path": f"{path}.id", "code": "INVALID", "message": f"{path}.id must be a short snake_case id"})
        elif identifier in ids:
            errors.append({"path": f"{path}.id", "code": "DUPLICATE", "message": f'{path}.id "{identifier}" is used twice'})
        ids.append(identifier)
        for lang in LANGUAGES:
            if utf16_len(_str(prop(prop(entry, "name"), lang))) > alimits["nameMax"]:
                errors.append({"path": f"{path}.name.{lang}", "code": "TOO_LONG", "message": f"{path} name ({lang}) over {alimits['nameMax']} characters"})
        if not js_trim(_str(prop(prop(entry, "name"), "en"))):
            errors.append({"path": f"{path}.name.en", "code": "REQUIRED", "message": f"{path} needs an English name"})
        if not _number_between(prop(entry, "watts"), 0, alimits["wattsMax"], strict_low=True):
            errors.append({"path": f"{path}.watts", "code": "INVALID", "message": f"{path} watts must be 1–{alimits['wattsMax']}"})
        if not _number_between(prop(entry, "defaultHours"), 0, alimits["hoursMax"]):
            errors.append({"path": f"{path}.defaultHours", "code": "INVALID", "message": f"{path} default hours must be 0–24"})

    rate = js_number(daily_gen_per_kw) if daily_gen_per_kw is not None else Decimal("NaN")
    generation = rate if rate.is_finite() and rate > 0 else FALLBACK_DAILY_GEN_PER_KW
    profiles = appliances["profiles"]
    for kw in js_keys(profiles):
        rows = profiles[kw]
        path = f'appliances.profiles["{kw}"]'
        size = js_number(kw)
        if size.is_nan() or not size > 0:
            errors.append({"path": path, "code": "INVALID", "message": f"{path}: profile key must be a kW number"})
            continue
        if not isinstance(rows, (list, tuple)):
            errors.append({"path": path, "code": "INVALID", "message": f"{path} must be a list of rows"})
            continue
        if len(rows) > alimits["profileRowsMax"]:
            errors.append({"path": path, "code": "TOO_MANY", "message": f"{path}: page 3 fits at most {alimits['profileRowsMax']} rows"})
        seen: list = []
        for index, row in enumerate(rows):
            row_path = f"{path}[{index + 1}]"
            identifier = prop(row, "id")
            if identifier not in ids:
                errors.append({"path": row_path, "code": "UNKNOWN_APPLIANCE", "message": f'{row_path}: "{js_string(identifier)}" is not in the appliance master'})
            if identifier in seen:
                errors.append({"path": row_path, "code": "DUPLICATE", "message": f'{row_path}: "{js_string(identifier)}" listed twice'})
            seen.append(identifier)
            if not _number_between(prop(row, "qty"), 0, alimits["qtyMax"]):
                errors.append({"path": f"{row_path}.qty", "code": "INVALID", "message": f"{row_path} quantity must be 0–{alimits['qtyMax']}"})
            if not _number_between(prop(row, "hours"), 0, alimits["hoursMax"]):
                errors.append({"path": f"{row_path}.hours", "code": "INVALID", "message": f"{row_path} hours must be 0–24"})
        total = total_units(materialise_rows(content, rows))
        with localcontext(EXACT):
            cap = round_places(size * generation * alimits["loadRatioMax"], 1)
            nameplate = round_places(size * generation, 1)
        if total > cap:
            errors.append(
                {
                    "path": path,
                    "code": "OVER_GENERATION",
                    "message": f"{kw} kW profile uses {number_text(total)} units/day — must stay under {number_text(cap)} (95% of {number_text(nameplate)} units the system generates)",
                    "total": total,
                    "cap": cap,
                }
            )
    return deep_freeze(errors)


_SECTION = re.compile(r"[.\[]")


def fit_summary(content: Any, daily_gen_per_kw: Any = None) -> Mapping:
    """``fitSummary`` — the ``fit_report``: ``{ok, errors, bySection}``."""
    errors = validate_content(content, daily_gen_per_kw)
    by_section: dict[str, list] = {}
    for error in errors:
        section = _SECTION.split(error["path"])[0] or "content"
        by_section.setdefault(section, []).append(error)
    return deep_freeze({"ok": not errors, "errors": errors, "bySection": by_section})


class ContentInvalid(ValueError):
    """``CONTENT_INVALID``: the content does not fit the page (``errors`` from :func:`validate_content`)."""

    code = "CONTENT_INVALID"

    def __init__(self, message: str, errors: Sequence[Mapping]) -> None:
        super().__init__(message)
        self.message = message
        self.errors = tuple(errors)


# ---------------------------------------------------------------------------------------------------------------------
# Content store lifecycle (pure: each function returns a new store)
# ---------------------------------------------------------------------------------------------------------------------


def create_empty_store(content: Any) -> Mapping:
    """Published v1 and a draft v2 holding a copy of the same content."""
    return deep_freeze(
        {
            "storeVersion": CONTENT_STORE_VERSION,
            "published": {"version": 1, "publishedAt": None, "publishedBy": None, "content": content},
            "draft": {"version": 2, "updatedAt": None, "updatedBy": None, "content": content},
        }
    )


def get_published(store: Any) -> Any:
    return prop(prop(store, "published"), "content") or None


def get_draft(store: Any) -> Any:
    return prop(prop(store, "draft"), "content") or None


def save_draft(store: Mapping, content: Any, *, actor_id: Any, at: str, daily_gen_per_kw: Any = None) -> Mapping:
    """Replace the draft; refused with :class:`ContentInvalid` when it does not fit."""
    errors = validate_content(content, daily_gen_per_kw)
    if errors:
        raise ContentInvalid("Content does not fit the page", errors)
    version = coalesce(prop(prop(store, "draft"), "version"), js_number(prop(prop(store, "published"), "version")) + 1)
    return deep_freeze({**store, "draft": {"version": version, "updatedAt": at, "updatedBy": actor_id if js_truthy(actor_id) else None, "content": content}})


def publish_draft(store: Mapping, *, actor_id: Any, at: str, daily_gen_per_kw: Any = None) -> Mapping:
    """Publish the draft as version n+1; the new draft (n+2) starts as a copy of it."""
    draft = prop(prop(store, "draft"), "content")
    errors = validate_content(draft, daily_gen_per_kw)
    if errors:
        raise ContentInvalid("Draft does not fit the page — fix before publishing", errors)
    version = int(js_number(prop(prop(store, "published"), "version") or 0)) + 1
    return deep_freeze(
        {
            **store,
            "published": {"version": version, "publishedAt": at, "publishedBy": actor_id if js_truthy(actor_id) else None, "content": draft},
            "draft": {"version": version + 1, "updatedAt": None, "updatedBy": None, "content": draft},
        }
    )


def discard_draft(store: Mapping) -> Mapping:
    """Reset the draft to the published content."""
    published = prop(store, "published")
    version = int(js_number(prop(published, "version") or 0)) + 1
    return deep_freeze({**store, "draft": {"version": version, "updatedAt": None, "updatedBy": None, "content": prop(published, "content")}})


def describe_store(store: Any) -> Mapping:
    """Version summary with ``unpublishedChanges`` (the draft's JSON differs from the published JSON)."""
    published, draft = prop(store, "published"), prop(store, "draft")
    same = to_json(prop(draft, "content")) == to_json(prop(published, "content"))
    return deep_freeze(
        {
            "published": {"version": prop(published, "version"), "publishedAt": prop(published, "publishedAt"), "publishedBy": prop(published, "publishedBy")},
            "draft": {"version": prop(draft, "version"), "updatedAt": prop(draft, "updatedAt"), "updatedBy": prop(draft, "updatedBy"), "unpublishedChanges": not same},
        }
    )


# ---------------------------------------------------------------------------------------------------------------------
# quotationBranding.js — bank / UPI (multi-account) and signature / seal (single slot), versioned
# ---------------------------------------------------------------------------------------------------------------------

BRANDING_KINDS = ("bank", "upi", "signature", "seal")
MULTI_ACCOUNT_KINDS = frozenset({"bank", "upi"})
BRANDING_SCHEMA_V1 = "flarize.quotation-branding/1"
BRANDING_SCHEMA_V2 = "flarize.quotation-branding/2"
_REQUIRED_VALUES: Mapping[str, tuple[str, ...]] = FrozenDict(bank=("bankName", "accountName", "accountNumber", "ifsc"), upi=("upiId",), signature=("signatoryName",), seal=())
_PREFIX: Mapping[str, str] = FrozenDict(bank="BK", upi="UPI", signature="SIG", seal="SEAL")


class BrandingError(ValueError):
    """A refused branding read or write: ``code`` is the legacy ``BRANDING_*`` code, ``detail`` its context."""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def _is_js_object(value: Any) -> bool:
    """``typeof value === 'object'`` for a JSON value (arrays included)."""
    return isinstance(value, (Mapping, list, tuple))


def _is_v1_kind_shape(block: Any) -> bool:
    if not _is_obj(block):
        return True
    if "primary" in block or ("accounts" in block and _is_obj(block["accounts"])):
        return False
    return "current" in block or "versions" in block


def _adapt_v1(block: Any) -> dict:
    current = coalesce(prop(block, "current"), None)
    versions = coalesce(prop(block, "versions"), {})
    if not js_truthy(current) and not js_keys(versions):
        return {"primary": None, "accounts": {}}
    return {"primary": "legacy", "accounts": {"legacy": {"accountId": "legacy", "label": "legacy", "status": "ACTIVE", "isDemo": False, "current": current, "versions": versions}}}


def _kind_view(store: Any, kind: Any) -> dict:
    if kind not in BRANDING_KINDS:
        raise BrandingError("BRANDING_KIND_INVALID", f'Unknown branding kind "{js_string(kind)}".')
    block = prop(store, kind)
    if _is_v1_kind_shape(block):
        return _adapt_v1(block)
    return {"primary": coalesce(block.get("primary", UNDEFINED), None), "accounts": coalesce(block.get("accounts", UNDEFINED), {})}


def current_version_id(store: Any, kind: str) -> Any:
    """The primary account's current version id for a kind, or ``None``."""
    view = _kind_view(store, kind)
    if not js_truthy(view["primary"]):
        return None
    return coalesce(prop(prop(view["accounts"], view["primary"]), "current"), None)


def get_version(store: Any, kind: str, version_id: str) -> Mapping:
    """A version record by id, searched across every account of the kind."""
    view = _kind_view(store, kind)
    for key in js_keys(view["accounts"]):
        record = prop(prop(view["accounts"][key], "versions"), version_id)
        if js_truthy(record):
            return record
    raise BrandingError("BRANDING_VERSION_NOT_FOUND", f'Branding {kind} version "{js_string(version_id)}" not found.', {"kind": kind, "versionId": version_id})


def _inactive(account: Any) -> bool:
    status = prop(account, "status")
    return js_truthy(status) and status != "ACTIVE"


def _resolve_selection(view: dict, kind: str, override: Any) -> dict | None:
    accounts = view["accounts"]
    if js_truthy(override) and _is_js_object(override):
        account_id, version_id = prop(override, "accountId"), prop(override, "versionId")
        if not js_truthy(account_id) or not isinstance(account_id, str):
            raise BrandingError("BRANDING_OVERRIDE_INVALID", f"Branding {kind} override object must carry a non-empty accountId.", {"kind": kind, "override": override})
        account = prop(accounts, account_id)
        if not js_truthy(account):
            raise BrandingError("BRANDING_ACCOUNT_NOT_FOUND", f'Branding {kind} account "{account_id}" not found.', {"kind": kind, "accountId": account_id})
        if _inactive(account):
            raise BrandingError(
                "BRANDING_ACCOUNT_INACTIVE",
                f'Branding {kind} account "{account_id}" is {js_string(account["status"])}; cannot select for a new quotation.',
                {"kind": kind, "accountId": account_id, "status": account["status"]},
            )
        target = version_id if js_truthy(version_id) else prop(account, "current")
        if not js_truthy(target):
            return None
        record = prop(prop(account, "versions"), target)
        if not js_truthy(record):
            raise BrandingError(
                "BRANDING_VERSION_NOT_FOUND",
                f'Branding {kind} account "{account_id}" version "{js_string(target)}" not found.',
                {"kind": kind, "accountId": account_id, "versionId": target},
            )
        return {"account": account, "record": record, "versionId": target, "source": "PROJECT_HEAD_OVERRIDE", "isDemo": js_truthy(prop(account, "isDemo")) or js_truthy(prop(record, "isDemo"))}

    if isinstance(override, str) and override:
        for key in js_keys(accounts):
            account = accounts[key]
            record = prop(prop(account, "versions"), override)
            if js_truthy(record):
                if _inactive(account):
                    raise BrandingError(
                        "BRANDING_ACCOUNT_INACTIVE",
                        f'Branding {kind} account "{js_string(prop(account, "accountId"))}" is {js_string(account["status"])}; cannot select for a new quotation.',
                        {"kind": kind, "accountId": prop(account, "accountId"), "status": account["status"]},
                    )
                return {
                    "account": account,
                    "record": record,
                    "versionId": override,
                    "source": "PROJECT_HEAD_OVERRIDE",
                    "isDemo": js_truthy(prop(account, "isDemo")) or js_truthy(prop(record, "isDemo")),
                }
        raise BrandingError("BRANDING_VERSION_NOT_FOUND", f'Branding {kind} version "{override}" not found in any account.', {"kind": kind, "versionId": override})

    if not js_truthy(view["primary"]):
        return None
    account = prop(accounts, view["primary"])
    if not js_truthy(account):
        return None
    if _inactive(account):
        raise BrandingError(
            "BRANDING_ACCOUNT_INACTIVE",
            f'Primary branding {kind} account "{js_string(prop(account, "accountId"))}" is {js_string(account["status"])}. Select an ACTIVE account explicitly.',
            {"kind": kind, "accountId": prop(account, "accountId"), "status": account["status"]},
        )
    target = prop(account, "current")
    if not js_truthy(target):
        return None
    record = prop(prop(account, "versions"), target)
    if not js_truthy(record):
        raise BrandingError(
            "BRANDING_VERSION_NOT_FOUND",
            f'Branding {kind} account "{js_string(prop(account, "accountId"))}" current version "{js_string(target)}" not found.',
            {"kind": kind, "accountId": prop(account, "accountId"), "versionId": target},
        )
    return {"account": account, "record": record, "versionId": target, "source": "CURRENT_VERSION", "isDemo": js_truthy(prop(account, "isDemo")) or js_truthy(prop(record, "isDemo"))}


def freeze_branding_snapshot(store: Any, overrides: Mapping | None = None) -> Mapping:
    """``freezeBrandingSnapshot``: per kind ``{accountId, versionId, publishedAt, publishedBy, source, isDemo, values}``
    or ``None`` when nothing is configured. An explicit refusal (unknown/inactive account, missing version) is fatal —
    never a silent fall back to the primary account."""
    overrides = overrides or {}
    out: dict[str, Any] = {}
    for kind in BRANDING_KINDS:
        view = _kind_view(store, kind)
        raw = overrides.get(kind, UNDEFINED)
        if kind not in MULTI_ACCOUNT_KINDS and js_truthy(raw) and _is_js_object(raw):
            raise BrandingError("BRANDING_OVERRIDE_INVALID", f"Branding {kind} is single-slot; override must be a versionId string, not an account object.", {"kind": kind, "override": raw})
        resolved = _resolve_selection(view, kind, raw)
        if resolved is None:
            out[kind] = None
            continue
        record = resolved["record"]
        out[kind] = {
            "accountId": coalesce(prop(resolved["account"], "accountId"), None),
            "versionId": resolved["versionId"],
            "publishedAt": coalesce(prop(record, "publishedAt"), None),
            "publishedBy": coalesce(prop(record, "publishedBy"), None),
            "source": resolved["source"],
            "isDemo": bool(resolved["isDemo"]),
            "values": coalesce(prop(record, "values"), None),
        }
    return deep_freeze(out)


def demo_branding_kinds(branding: Mapping | None) -> tuple[str, ...]:
    """The resolved kinds flagged ``isDemo`` (a production issue refuses them)."""
    if not branding:
        return ()
    return tuple(kind for kind in BRANDING_KINDS if js_truthy(branding.get(kind)) and branding[kind].get("isDemo") is True)


def list_accounts(store: Any, kind: str) -> tuple:
    """Account summaries for a kind (a v1 store appears as its synthetic ``legacy`` account)."""
    view = _kind_view(store, kind)
    out = []
    for key in js_keys(view["accounts"]):
        account = view["accounts"][key]
        out.append(
            {
                "accountId": coalesce(prop(account, "accountId"), None),
                "label": coalesce(prop(account, "label"), None),
                "status": coalesce(prop(account, "status"), "ACTIVE"),
                "isDemo": js_truthy(prop(account, "isDemo")),
                "current": coalesce(prop(account, "current"), None),
                "versionCount": len(js_keys(prop(account, "versions") or {})),
                "isPrimary": prop(account, "accountId") == view["primary"],
            }
        )
    return deep_freeze(out)


def kind_contains_demo(store: Any, kind: str) -> bool:
    view = _kind_view(store, kind)
    for key in js_keys(view["accounts"]):
        account = view["accounts"][key]
        if js_truthy(prop(account, "isDemo")):
            return True
        versions = prop(account, "versions") or {}
        if any(js_truthy(prop(versions[name], "isDemo")) for name in js_keys(versions)):
            return True
    return False


def _clone_store(store: Any) -> dict:
    """The write-side normal form: schema v2; bank/UPI as accounts (a v1 block becomes the ``legacy`` account)."""
    clone = thaw(store) if _is_obj(store) else {}
    clone.setdefault("schema", None)
    if not js_truthy(clone["schema"]):
        clone["schema"] = BRANDING_SCHEMA_V2
    for kind in ("bank", "upi"):
        if not _is_obj(clone.get(kind)):
            clone[kind] = {"primary": None, "accounts": {}}
        if _is_v1_kind_shape(clone[kind]):
            clone[kind] = thaw(_adapt_v1(clone[kind]))
        if not js_truthy(clone[kind].get("accounts")):
            clone[kind]["accounts"] = {}
    for kind in ("signature", "seal"):
        if not _is_obj(clone.get(kind)):
            clone[kind] = {"current": None, "versions": {}}
        if not js_truthy(clone[kind].get("versions")):
            clone[kind]["versions"] = {}
    return clone


def _assert_values(kind: str, values: Any) -> None:
    if not _is_obj(values):
        raise BrandingError("BRANDING_VERSION_NOT_FOUND", f"values object is required for {kind}.")
    missing = [name for name in _REQUIRED_VALUES[kind] if is_nullish(values.get(name)) or values.get(name) == ""]
    if missing:
        raise BrandingError("BRANDING_VERSION_NOT_FOUND", f"Branding {kind} values missing: {', '.join(missing)}.", {"kind": kind, "missing": missing})


def publish_branding_version(
    store: Any,
    *,
    actor_id: str,
    kind: str,
    values: Any,
    at: str,
    account_id: str | None = None,
    label: str | None = None,
    is_demo: bool = False,
    version_id: str | None = None,
    make_primary: bool = False,
) -> Mapping:
    """Publish an immutable version (bank/UPI: into ``account_id``, created on first use; signature/seal: the slot)."""
    if kind not in BRANDING_KINDS:
        raise BrandingError("BRANDING_KIND_INVALID", f'Unknown branding kind "{js_string(kind)}".')
    if not at:
        raise BrandingError("BRANDING_VERSION_NOT_FOUND", "Timestamp (at) is required.")
    _assert_values(kind, values)
    clone = _clone_store(store)
    default_id = f"{_PREFIX[kind]}-{re.sub(r'[^A-Za-z0-9_-]', '', account_id or 'slot')}-{re.sub(r'[^0-9]', '', at)[:17]}"
    record = {"versionId": version_id or default_id, "publishedAt": at, "publishedBy": actor_id, "isDemo": bool(is_demo), "values": thaw(values)}
    if kind in MULTI_ACCOUNT_KINDS:
        if not account_id:
            raise BrandingError("BRANDING_ACCOUNT_NOT_FOUND", f"accountId is required for {kind}.")
        block = clone[kind]
        account = block["accounts"].get(account_id) or {"accountId": account_id, "label": label or account_id, "status": "ACTIVE", "isDemo": bool(is_demo), "current": None, "versions": {}}
        if record["versionId"] in account["versions"]:
            raise BrandingError("BRANDING_VERSION_NOT_FOUND", f'Version "{record["versionId"]}" already exists; versions are immutable.')
        if label:
            account["label"] = label
        account["isDemo"] = bool(is_demo) or bool(account.get("isDemo"))
        account["versions"][record["versionId"]] = record
        account["current"] = record["versionId"]
        block["accounts"][account_id] = account
        if not block.get("primary") or make_primary:
            block["primary"] = account_id
    else:
        block = clone[kind]
        if record["versionId"] in block["versions"]:
            raise BrandingError("BRANDING_VERSION_NOT_FOUND", f'Version "{record["versionId"]}" already exists; versions are immutable.')
        block["versions"][record["versionId"]] = record
        block["current"] = record["versionId"]
    return deep_freeze(clone)


def set_branding_account_status(store: Any, *, actor_id: str, kind: str, account_id: str, status: str, at: str) -> Mapping:
    """ACTIVE ⇄ INACTIVE for a bank/UPI account (never deleted); an inactive primary hands over to another ACTIVE one."""
    if not at:
        raise BrandingError("BRANDING_VERSION_NOT_FOUND", "Timestamp (at) is required.")
    if kind not in MULTI_ACCOUNT_KINDS:
        raise BrandingError("BRANDING_KIND_INVALID", f"{js_string(kind)} is single-slot; account status does not apply.")
    if status not in ("ACTIVE", "INACTIVE"):
        raise BrandingError("BRANDING_KIND_INVALID", f'status must be ACTIVE or INACTIVE (got "{js_string(status)}").')
    clone = _clone_store(store)
    account = clone[kind]["accounts"].get(account_id)
    if not account:
        raise BrandingError("BRANDING_VERSION_NOT_FOUND", f'{kind} account "{js_string(account_id)}" not found.')
    account.update(status=status, statusChangedBy=actor_id, statusChangedAt=at)
    if status == "INACTIVE" and clone[kind]["primary"] == account_id:
        successor = next((item for item in clone[kind]["accounts"].values() if item.get("status") == "ACTIVE" and item.get("accountId") != account_id), None)
        clone[kind]["primary"] = successor["accountId"] if successor else None
    return deep_freeze(clone)


def set_primary_branding_account(store: Any, *, actor_id: str, kind: str, account_id: str, at: str) -> Mapping:
    """The ACTIVE bank/UPI account new quotations use by default."""
    if not at:
        raise BrandingError("BRANDING_VERSION_NOT_FOUND", "Timestamp (at) is required.")
    if kind not in MULTI_ACCOUNT_KINDS:
        raise BrandingError("BRANDING_KIND_INVALID", f"{js_string(kind)} is single-slot; primary does not apply.")
    clone = _clone_store(store)
    account = clone[kind]["accounts"].get(account_id)
    if not account:
        raise BrandingError("BRANDING_VERSION_NOT_FOUND", f'{kind} account "{js_string(account_id)}" not found.')
    if account.get("status") != "ACTIVE":
        raise BrandingError("BRANDING_KIND_INVALID", f'{kind} account "{account_id}" is not ACTIVE.')
    clone[kind].update(primary=account_id, primaryChangedBy=actor_id, primaryChangedAt=at)
    return deep_freeze(clone)


def describe_branding_store(store: Any) -> Mapping:
    """Editor summary per kind: multi-account, primary, accounts, published, containsDemo."""
    kinds = {}
    for kind in BRANDING_KINDS:
        accounts = list_accounts(store, kind)
        kinds[kind] = {
            "multiAccount": kind in MULTI_ACCOUNT_KINDS,
            "primary": _kind_view(store, kind)["primary"],
            "accounts": accounts,
            "published": any(js_truthy(account["current"]) for account in accounts),
            "containsDemo": kind_contains_demo(store, kind),
        }
    return deep_freeze({"schema": coalesce(prop(store, "schema"), None), "kinds": kinds})
