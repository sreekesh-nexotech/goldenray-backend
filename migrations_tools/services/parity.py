"""Delivery and calculator parity (PLAN §7.6 #3, #5, #10): legacy responses vs the new public API.

The only normalisations are the documented ones:

* collections (``/api/<collection>``): ``updatedAt`` is dropped everywhere (PLAN §7.6 #3);
* FAQs: each legacy integer ``id`` becomes the uid its row was imported as (PLAN §6.3);
* job positions: the integer ``id`` becomes ``uid`` (DV-47);
* calculators: a legacy 500 is a 400 ``invalid_input``, errors use the envelope's ``message`` (docs/decisions/
  calculators-emi.md); EMI ids become uids through ``core_legacy_map`` (DV-86).
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from django.conf import settings

from core.models import LegacyMap

NEW = "/api/public/v1"
UNKNOWN_UID = str(uuid.UUID(int=0))


def uid_map(system: str, source_table: str, model) -> dict[int, str]:
    targets = dict(LegacyMap.objects.filter(source_system=system, source_table=source_table).values_list("target_id", "source_id"))
    return {int(targets[pk]): str(uid) for pk, uid in model.all_objects.filter(pk__in=targets).values_list("pk", "uid")}


def drop_key(value, key: str):
    if isinstance(value, dict):
        return {name: drop_key(item, key) for name, item in value.items() if name != key}
    if isinstance(value, list):
        return [drop_key(item, key) for item in value]
    return value


def first_difference(expected, got, path: str = "$") -> str | None:
    """A short description of where two JSON values differ (``None`` when equal, ints and floats kept apart)."""
    if isinstance(expected, dict) and isinstance(got, dict):
        for key in list(dict.fromkeys([*expected, *got])):
            if key not in got:
                return f"{path}.{key} missing in the new payload"
            if key not in expected:
                return f"{path}.{key} only in the new payload"
            found = first_difference(expected[key], got[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(expected, list) and isinstance(got, list):
        if len(expected) != len(got):
            return f"{path}: {len(expected)} items in the legacy payload, {len(got)} in the new"
        for index, (left, right) in enumerate(zip(expected, got, strict=True)):
            found = first_difference(left, right, f"{path}[{index}]")
            if found:
                return found
        return None
    if type(expected) is not type(got) or expected != got:
        return f"{path}: legacy {json.dumps(expected, ensure_ascii=False, default=str)[:160]} ≠ new {json.dumps(got, ensure_ascii=False, default=str)[:160]}"
    return None


class Case:
    """One compared request."""

    def __init__(self, name: str, legacy: tuple, new: tuple, *, expected=None):
        self.name = name
        self.legacy_status, legacy_body = legacy
        self.new_status, self.new_body = new
        self.expected = legacy_body if expected is None else expected

    @property
    def difference(self) -> str | None:
        if self.legacy_status != self.new_status:
            return f"status {self.legacy_status} → {self.new_status}"
        if self.legacy_status != 200:
            return None
        return first_difference(self.expected, self.new_body)


# ── CMS delivery ─────────────────────────────────────────────────────────────────────────────────────────────────
def collection_cases(source, legacy, new) -> list[Case]:
    cases = []
    collections = {row["id"]: row["api_uid"] for row in source.rows("catalog_collection")}
    for api_uid in collections.values():
        params = [("populate", "*"), ("pagination[pageSize]", "100")]
        cases.append(_collection_case(f"{api_uid} list", legacy, new, api_uid, params))
    for row in source.rows("content_entry"):
        if (row.get("status") or "").lower() != "published" or row.get("collection_id") not in collections:
            continue
        api_uid = collections[row["collection_id"]]
        params = [("populate", "*"), ("filters[slug][$eq]", row["slug"])]
        cases.append(_collection_case(f"{api_uid}/{row['slug']}", legacy, new, api_uid, params))
    return cases


def _collection_case(name, legacy, new, api_uid, params) -> Case:
    old = legacy.get(f"/api/{api_uid}", params)
    got = new.get(f"{NEW}/content/{api_uid}/", params)
    return Case(name, (old[0], drop_key(old[1], "updatedAt")), (got[0], drop_key(got[1], "updatedAt")))


def page_cases(source, legacy, new) -> list[Case]:
    cases = []
    for row in source.rows("sitepages_page"):
        route = row["route"]
        cases.append(Case(f"page-content {route}", legacy.get("/api/page-content", {"route": route}), new.get(f"{NEW}/pages/", {"route": route})))
    return cases


def faq_cases(source, legacy, new) -> list[Case]:
    from faqs.models import Faq

    uids = uid_map("CMS", "faqs_faq", Faq)
    pages = {row["id"]: row["route"] for row in source.rows("sitepages_page")}
    queries: dict[tuple, None] = {}
    for row in source.rows("faqs_faq"):
        route = pages.get(row.get("page_id"))
        if route is None:
            continue
        queries[(route, None)] = None
        queries[(route, row.get("section") or "")] = None
    cases = []
    for route, section in queries:
        legacy_params = {"page": route, **({"section": section} if section is not None else {})}
        new_params = {"route": route, **({"section": section} if section is not None else {})}
        old = legacy.get("/api/faqs", legacy_params)
        expected = old[1]
        if old[0] == 200 and isinstance(expected, dict) and isinstance(expected.get("data"), list):
            expected = {**expected, "data": [{**item, "id": uids.get(item.get("id"), item.get("id"))} for item in expected["data"]]}
        cases.append(Case(f"faqs {route} {section if section is not None else ''}".rstrip(), old, new.get(f"{NEW}/faqs/", new_params), expected=expected))
    return cases


def position_cases(source, legacy, new) -> list[Case]:
    from careers.models import JobPosition

    uids = uid_map("CMS", "careers_job_position", JobPosition)

    def expected(body):
        if not isinstance(body, dict) or "data" not in body:
            return body

        def card(item):
            item = dict(item)
            if "id" in item:
                item["uid"] = uids.get(item.pop("id"), UNKNOWN_UID)
            return item

        data = body["data"]
        return {**body, "data": [card(item) for item in data] if isinstance(data, list) else card(data)}

    def case(name, legacy_path, new_path, params=None):
        old = legacy.get(legacy_path, params)
        return Case(name, old, new.get(new_path, params), expected=expected(old[1]) if old[0] == 200 else None)

    cases = [case("job-positions", "/api/job-positions", f"{NEW}/job-positions/")]
    for row in source.rows("careers_department"):
        cases.append(case(f"job-positions ?department={row['slug']}", "/api/job-positions", f"{NEW}/job-positions/", {"department": row["slug"]}))
    for row in source.rows("careers_job_position"):
        cases.append(case(f"job-positions/{row['slug']}", f"/api/job-positions/{row['slug']}", f"{NEW}/job-positions/{row['slug']}/"))
    return cases


# ── Historical slugs (PLAN §7.6 #5) ──────────────────────────────────────────────────────────────────────────────
def slug_problems(source, new) -> tuple[int, list[str]]:
    """Every historical and current published slug resolves (200, or 301) on the new API."""
    collections = {row["id"]: row["api_uid"] for row in source.rows("catalog_collection")}
    entries = {row["id"]: row for row in source.rows("content_entry")}
    checks = []
    for row in source.rows("content_entry_slug_history"):
        entry = entries.get(row.get("entry_id"))
        if row.get("is_active", True) and entry and (entry.get("status") or "").lower() == "published":
            checks.append((f"alias {row['slug']}", f"{NEW}/content/{collections.get(row.get('collection_id') or entry['collection_id'])}/{row['slug']}/"))
    for entry in entries.values():
        if (entry.get("status") or "").lower() == "published":
            checks.append((f"entry {entry['slug']}", f"{NEW}/content/{collections.get(entry['collection_id'])}/{entry['slug']}/"))
    for row in source.rows("sitepages_page"):
        if (row.get("status") or "").lower() == "published":
            checks.append((f"page {row['route']}", (f"{NEW}/pages/", {"route": row["route"]})))
    for row in source.rows("careers_job_position"):
        if (row.get("status") or "").lower() == "published":
            checks.append((f"position {row['slug']}", f"{NEW}/job-positions/{row['slug']}/"))
    problems = []
    for name, target in checks:
        path, params = target if isinstance(target, tuple) else (target, None)
        status, body = new.get(path, params)
        empty = status == 200 and isinstance(body, dict) and body.get("data") in ([], None)
        if status not in (200, 301) or empty:
            problems.append(f"{name}: {path} → {status}{' (no data)' if empty else ''}")
    return len(checks), problems


# ── Calculators (PLAN §7.6 #10) ──────────────────────────────────────────────────────────────────────────────────
CALCULATOR_ENDPOINTS = {"basic": f"{NEW}/calculators/basic/", "basic_v2": f"{NEW}/calculators/basic-v2/", "advanced": f"{NEW}/calculators/advanced/"}
EMI_ENDPOINTS = {"calculate": f"{NEW}/calculators/emi/", "quotation": f"{NEW}/calculators/emi/quotation/"}


def default_corpus_dirs() -> dict[str, Path]:
    root = Path(settings.BASE_DIR)
    return {"calculators": root / "calculators/tests/parity/uat", "emi": root / "emi/tests/parity/uat"}


def typed(value):
    if isinstance(value, dict):
        return {key: typed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [typed(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return ("float", repr(value))
    if isinstance(value, int):
        return ("int", value)
    return value


def compare_calculator(case: dict, status: int, body, *, canonical=None, message=None) -> str | None:
    legacy_status, legacy_body = case["status"], case["response"]
    if legacy_status >= 500:
        return None if status == 400 and isinstance(body, dict) and body.get("code") == "invalid_input" else f"legacy {legacy_status} → {status}"
    if status != legacy_status:
        return f"legacy {legacy_status} → {status}"
    if legacy_status != 200:
        expected = legacy_body.get("error") if isinstance(legacy_body, dict) else None
        expected = message(expected) if message and expected else expected
        got = body.get("message") if isinstance(body, dict) else None
        return None if expected == got else f"message {expected!r} → {got!r}"
    expected = canonical(legacy_body) if canonical else legacy_body
    return None if typed(expected) == typed(body) else first_difference(expected, body) or "number types differ"


class EmiTranslation:
    """Legacy EMI ids ↔ uids (the ``/legacy/`` shim's translation, DV-86)."""

    def __init__(self):
        from emi.models import Bank, InterestRateRule, SystemSize

        self.maps = {
            "emi_system_size": uid_map("BACKEND", "emi_system_size", SystemSize),
            "emi_interest_rate_rule": uid_map("BACKEND", "emi_interest_rate_rule", InterestRateRule),
            "emi_bank": uid_map("BACKEND", "emi_bank", Bank),
        }

    def request(self, body):
        if not isinstance(body, dict):
            return body
        body = dict(body)
        legacy = body.get("size_id") or body.get("installation_id") or body.get("id")
        for key in ("size_id", "installation_id", "id"):
            body.pop(key, None)
        if legacy is None or legacy == "":
            return body
        try:
            legacy_id = int(legacy)
        except (TypeError, ValueError):
            body["size_uid"] = "not-a-uid"
            return body
        body["size_uid"] = self.maps["emi_system_size"].get(legacy_id, UNKNOWN_UID)
        return body

    def response(self, value, *, table: str | None = None):
        if isinstance(value, list):
            return [self.response(item, table=table) for item in value]
        if not isinstance(value, dict):
            return value
        out = {}
        for key, item in value.items():
            if key == "size_id":
                out["size_uid"] = None if item is None else self.maps["emi_system_size"].get(item, UNKNOWN_UID)
            elif key == "rule_id":
                out["rule_uid"] = None if item is None else self.maps["emi_interest_rate_rule"].get(item, UNKNOWN_UID)
            elif key == "id" and table:
                out["uid"] = self.maps[table].get(item, UNKNOWN_UID)
            else:
                out[key] = self.response(item)
        return out

    @staticmethod
    def message(text: str) -> str:
        return text.replace("size_id", "size_uid")


def _dumps(value) -> str:
    """JSON text keeping every recorded number's text (``1e400`` stays ``1e400``)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(key, ensure_ascii=False)}: {_dumps(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_dumps(item) for item in value) + "]"
    if isinstance(value, Decimal):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


@contextmanager
def list_prices_as_release():
    """Rehearsal mode: price website products (the advanced calculator's hybrid batteries) from the imported current
    LIST rows, as PriceRelease #1 would, while no release can be published yet (the market-rate set and GST keys come
    from the Flarize import / the business). The previous provider and the caches are restored afterwards."""
    from catalog.services import pricing_hooks
    from flarize.cache_utils import bump
    from pricing.services.prices import current_row

    def provider(components):
        prices = {}
        for component in components:
            row = current_row(component, "LIST")
            if row is not None and row.amount > 0:
                prices[component.pk] = pricing_hooks.PriceInfo(min_amount=row.amount, max_amount=row.amount)
        return prices

    previous = pricing_hooks.current_provider()
    pricing_hooks.register(provider)
    bump("pricing")
    try:
        yield
    finally:
        pricing_hooks.register(previous)
        bump("pricing")


def calculator_differences(new, dirs: dict[str, Path]) -> tuple[int, list[str]]:
    """Replay the committed corpora against the new endpoints: ``(cases replayed, differences)``."""
    replayed, differences = 0, []
    for key, path in CALCULATOR_ENDPOINTS.items():
        for case in json.loads((dirs["calculators"] / f"corpus_{key}.json").read_text())["cases"]:
            replayed += 1
            status, body = new.post(path, case["request"].encode("utf-8"))
            difference = compare_calculator(case, status, body)
            if difference:
                differences.append(f"{key} {case['name']}: {difference}")
    emi = EmiTranslation()
    for key, path in EMI_ENDPOINTS.items():
        for case in json.loads((dirs["emi"] / f"corpus_{key}.json").read_text())["cases"]:
            replayed += 1
            text = case["request"]
            if key == "calculate":
                try:
                    text = _dumps(emi.request(json.loads(text, parse_float=Decimal)))
                except ValueError:
                    pass  # not JSON: sent as recorded
            status, body = new.post(path, text.encode("utf-8"))
            difference = compare_calculator(case, status, body, canonical=emi.response, message=emi.message)
            if difference:
                differences.append(f"emi {key} {case['name']}: {difference}")
    config = json.loads((dirs["emi"] / "config.json").read_text())["response"]
    expected = {
        "settings": {**config["settings"], "disclaimer_en": "", "disclaimer_ml": ""},
        "system_sizes": emi.response(config["system_sizes"], table="emi_system_size"),
        "banks": emi.response(config["banks"], table="emi_bank"),
    }
    replayed += 1
    status, body = new.get(f"{NEW}/calculators/emi/config/")
    difference = compare_calculator({"status": 200, "response": expected}, status, body)
    if difference:
        differences.append(f"emi config: {difference}")
    return replayed, differences
