"""Pre-fill one fragment per inventory row from the OpenAPI schema (host python3, stdlib only).

    python3 docs/postman/_work/seed_fragments.py                     # (re)write seeds/ — Phase 2 input
    python3 docs/postman/_work/seed_fragments.py --sync-fragments    # copy inventory-owned fields into fragments/
    python3 docs/postman/_work/seed_fragments.py --add-missing       # start fragments for new endpoints from their seeds

Writes ``seeds/<group>.json``. Phase-2 subagents read the code for every row, correct and enrich these seeds and write
``fragments/<group>.json`` (the build only reads fragments). Two kinds of rows are complete by construction and marked
``"auto": true`` (they are copied to the fragments unchanged unless an agent finds them wrong):

* ``refused`` rows — a routed method that can never succeed (PUT on a PATCH-only ViewSet → 403 unmapped / 405);
* legacy ``AppendSlashView`` rows — the slash-less old URL that answers 301 to the slashed one.

Fragment shape (see README "Fragment format"):
``{id, method, path, app, surface, view, action, group, folder[], summary, description, auth{scheme, permission,
scope, notes}, headers[], path_params[{name, type, variable, example, description}], query_params[{key, value,
description, required}], body{mode, content_type, raw|formdata[]}|null, success_status, responses[{status,
description, example}], side_effects[], capture[{var, from}], order, refused, flag, auto, unresolved[]}``
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

WORK = Path(__file__).resolve().parent
SEEDS = WORK / "seeds"

# Phase-2 partitions: one subagent each (≈50–80 rows, auto rows excluded from the reading load).
GROUPS = {
    "A_platform": ["accounts", "core", "audit", "media", "documents", "company", "django"],
    "B_product_master": ["catalog", "inventory", "procurement"],
    "C_pricing_engineering": ["pricing", "engineering"],
    "D_bom_packs": ["bom", "packs"],
    "E_quotations_agreements_customers": ["quotations", "agreements", "customers"],
    "F_leads_projects": ["leads", "projects"],
    "G_site_inspections": ["site_inspections"],
    "H_blog": ["blog"],
    "I_sitepages_faqs_seo": ["sitepages", "faqs", "seo"],
    "J_careers_calculators_emi": ["careers", "calculators", "emi"],
    "K_reference": ["reference"],
    "L_hr_attendance": ["hr", "attendance"],
    "M_devices": ["devices"],
    "N_legacy": ["legacy"],
}
APP_GROUP = {app: group for group, apps in GROUPS.items() for app in apps}
SURFACE_PREFIX = {"staff": "/api/v1/", "public": "/api/public/v1/", "agent": "/api/agent/v1/", "customer": "/api/customer/v1/"}
ACTION_ORDER = {"list": 10, "create": 20, "retrieve": 30, "update": 40, "partial_update": 50, "destroy": 90}
SECURITY_SCHEME = {"staffJWT": "staff_jwt", "serviceToken": "agent_token"}
PLACEHOLDER_UUID = "00000000-0000-4000-8000-000000000000"


def resolve(schema: dict, components: dict, depth: int = 0) -> dict:
    while "$ref" in schema:
        schema = components[schema["$ref"].rsplit("/", 1)[-1]]
    if "allOf" in schema:
        merged = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            part = resolve(part, components, depth)
            merged["properties"].update(part.get("properties", {}))
            merged["required"] += part.get("required", [])
            for key in ("enum", "type", "format"):
                if key in part and key not in ("type",):
                    merged[key] = part[key]
            if part.get("type") and part.get("type") != "object" and not part.get("properties"):
                return {**part, **{k: v for k, v in schema.items() if k not in ("allOf",)}}
        return merged
    for key in ("oneOf", "anyOf"):
        if key in schema:
            options = [resolve(s, components, depth) for s in schema[key]]
            options = [o for o in options if o.get("type") != "null"] or options
            return options[0]
    return schema


def example_for(schema: dict, components: dict, name: str = "", depth: int = 0, request: bool = True):
    schema = resolve(schema, components, depth)
    if "example" in schema:
        return schema["example"]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    if schema.get("enum"):
        values = [v for v in schema["enum"] if v not in (None, "")]
        return values[0] if values else None
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    fmt = schema.get("format", "")
    if kind == "object" or "properties" in schema:
        if depth > 4:
            return {}
        out = {}
        required = set(schema.get("required", []))
        for prop, sub in schema.get("properties", {}).items():
            sub_resolved = resolve(sub, components, depth + 1)
            if request and (sub.get("readOnly") or sub_resolved.get("readOnly")):
                continue
            if not request and (sub.get("writeOnly") or sub_resolved.get("writeOnly")):
                continue
            if request and prop not in required and depth > 1:
                continue
            out[prop] = example_for(sub, components, prop, depth + 1, request)
        if not schema.get("properties") and isinstance(schema.get("additionalProperties"), dict):
            return {"key": example_for(schema["additionalProperties"], components, name, depth + 1, request)}
        return out
    if kind == "array":
        item = example_for(schema.get("items", {}), components, name, depth + 1, request)
        return [item] if item not in (None, {}) or depth < 3 else []
    if kind == "integer":
        return schema.get("minimum", 1) if isinstance(schema.get("minimum"), int) and schema.get("minimum") > 0 else 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    if fmt == "uuid" or name == "uid" or name.endswith("_uid"):
        return PLACEHOLDER_UUID
    if fmt == "date-time":
        return "2026-10-01T09:00:00+05:30"
    if fmt == "date":
        return "2026-10-01"
    if fmt == "time":
        return "09:00:00"
    if fmt == "email" or "email" in name:
        return "user@example.com"
    if fmt in ("uri", "url") or name.endswith("url"):
        return "https://example.com/"
    if fmt == "binary":
        return None
    if fmt == "decimal" or (schema.get("pattern") or "").startswith("^-?\\d"):
        return "100.00"
    if "phone" in name or "mobile" in name:
        return "+919876543210"
    if name in ("slug", "key", "code"):
        return "sample-" + name
    if name == "password" or name.endswith("_password"):
        return "{{staff_password}}"
    return f"sample {name}".strip() if name else "string"


def binary_fields(schema: dict, components: dict) -> set:
    schema = resolve(schema, components)
    out = set()
    for prop, sub in schema.get("properties", {}).items():
        sub = resolve(sub, components)
        if sub.get("format") == "binary" or (sub.get("type") == "array" and resolve(sub.get("items", {}), components).get("format") == "binary"):
            out.add(prop)
    return out


def field_list(schema: dict, components: dict) -> list:
    schema = resolve(schema, components)
    required = set(schema.get("required", []))
    fields = []
    for prop, sub in schema.get("properties", {}).items():
        sub_r = resolve(sub, components)
        if sub.get("readOnly") or sub_r.get("readOnly"):
            continue
        kind = sub_r.get("type", "object" if "properties" in sub_r else "")
        info = {"name": prop, "type": kind + (f":{sub_r['format']}" if sub_r.get("format") else ""), "required": prop in required}
        if sub_r.get("enum"):
            info["choices"] = [v for v in sub_r["enum"] if v is not None]
        if sub_r.get("nullable"):
            info["nullable"] = True
        fields.append(info)
    return fields


def variable_for(path: str, surface: str, param: str) -> str:
    """Stable variable name for a path param: the literal segments before it + ``_uid`` (or ``_<param>``)."""
    prefix = SURFACE_PREFIX.get(surface, "/")
    rest = path[len(prefix) :] if path.startswith(prefix) else path.lstrip("/")
    segments = []
    for seg in rest.split("/"):
        if seg == "{" + param + "}":
            break
        if seg and not seg.startswith("{"):
            segments.append(seg)
    if surface != "staff" and segments and segments[0] == surface:
        segments = segments[1:]
    tag = re.sub(r"[^a-z0-9]+", "_", surface)
    if surface == "dev-media":
        segments, tag = [], "dev_media"
    base = "_".join(re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") for s in segments)
    if surface != "staff":
        base = f"{tag}_{base}" if base else tag
    base = base or "root"
    suffix = "uid" if param == "uid" or param.endswith("_uid") else param
    return f"{base}_{suffix}"


def collection_variable(path: str, surface: str) -> str:
    """Variable a create on collection ``path`` should capture its ``uid`` into (matches detail-route variables)."""
    return variable_for(path.rstrip("/") + "/{uid}/", surface, "uid")


def default_folder(row: dict) -> list:
    prefix = SURFACE_PREFIX.get(row["surface"], "/")
    rest = row["path"][len(prefix) :] if row["path"].startswith(prefix) else row["path"].lstrip("/")
    literals = [s for s in rest.split("/") if s and not s.startswith("{")]
    resource = "/".join(literals[:2]) if len(literals) > 1 and row["action"] not in (None,) else (literals[0] if literals else "root")
    return [row["app"], resource]


def param_example(name: str, ptype: str, fixed) -> str:
    if isinstance(fixed, list):
        return fixed[0]
    if ptype == "uuid" or name == "uid" or name.endswith("_uid") or ptype == "regex" and name == "uid":
        return PLACEHOLDER_UUID
    return f"sample-{name}"


def refused_fields(row: dict) -> dict:
    """Summary/description/responses of a routed method that never succeeds (``row["refused"]`` from dump_urls.py)."""
    status = int(row["refused"][:3])
    if status == 403:
        text = (
            f"Routed by the DRF router but refused for every caller: {row['refused']}. DRF checks permissions in `initial()` "
            "before it looks at `http_method_names`, and `core.permissions.HasModulePermission` denies any action the view's "
            "`action_permissions` does not map. Edit the record with PATCH instead."
        )
        responses = [
            {
                "status": 403,
                "description": "permission_denied (every authenticated caller)",
                "example": {"code": "permission_denied", "message": "You do not have permission to perform this action.", "errors": None, "error_codes": None},
            },
            {"status": 401, "description": "not_authenticated (no/invalid token)"},
        ]
    else:
        text = f"Routed but never succeeds: {row['refused']}. A caller holding the mapped permission gets 405; anyone else gets 403. Edit with PATCH instead."
        responses = [
            {
                "status": 405,
                "description": "method_not_allowed (caller holds the mapped permission)",
                "example": {"code": "method_not_allowed", "message": 'Method "PUT" not allowed.', "errors": None, "error_codes": None},
            },
            {"status": 403, "description": "permission_denied (caller lacks the permission)"},
            {"status": 401, "description": "not_authenticated"},
        ]
    return {"summary": f"Not supported ({status}): use PATCH", "description": text, "success_status": None, "responses": responses, "refused": row["refused"]}


SYNCED_FROM_INVENTORY = ("method", "path", "app", "surface", "view", "action", "source_file", "refused", "flag")


def sync_fragments():
    """After Phase 2: copy the fields the inventory owns into every fragment (agents must not change them) and
    regenerate the text of refused rows, so a dump_urls.py fix propagates without re-running the agents."""
    inventory = {r["id"]: r for r in json.loads((WORK / "inventory.json").read_text())}
    changed = 0
    for file in sorted((WORK / "fragments").glob("*.json")):
        frags = json.loads(file.read_text())
        for f in frags:
            row = inventory.get(f["id"])
            if row is None:
                continue
            before = json.dumps(f, sort_keys=True)
            for key in SYNCED_FROM_INVENTORY:
                f[key] = row.get(key)
            if row["refused"]:
                f.update(refused_fields(row))
                f["auth"] = {**f.get("auth", {}), "scheme": "staff_jwt"}
                f["capture"] = []
            changed += before != json.dumps(f, sort_keys=True)
        file.write_text(json.dumps(frags, indent=1, ensure_ascii=False) + "\n")
    print(f"synced fragments from inventory: {changed} changed")


def add_missing():
    """For inventory rows that have no fragment yet (a new endpoint): append the seed to ``fragments/<group>.json`` with an
    ``unresolved`` note, so the build stays complete; then enrich it from code (AGENT_BRIEF.md) and remove the note."""
    have = set()
    for file in (WORK / "fragments").glob("*.json"):
        have |= {f["id"] for f in json.loads(file.read_text())}
    added = 0
    for seed_file in sorted(SEEDS.glob("*.json")):
        new = [f for f in json.loads(seed_file.read_text()) if f["id"] not in have]
        if not new:
            continue
        for f in new:
            f["summary"] = f["summary"] or f"{f['method']} {f['path']}"
            f["auth"]["scheme"] = f["auth"]["scheme"] or "none"
            f["success_status"] = f["success_status"] or (None if f["refused"] else 200)
            if f["method"] in ("POST", "PUT", "PATCH") and f["body"] is None:
                f["no_body"] = True
            if not f["auto"]:
                f["unresolved"].append("seed only: not yet verified against the code (see AGENT_BRIEF.md)")
        target = WORK / "fragments" / seed_file.name
        frags = json.loads(target.read_text()) if target.exists() else []
        target.write_text(json.dumps(frags + new, indent=1, ensure_ascii=False) + "\n")
        added += len(new)
        print(f"{seed_file.stem}: added {len(new)}: " + ", ".join(f["id"] for f in new))
    print(f"added {added} seed fragments for new endpoints")


def seed_row(row: dict, schema: dict) -> dict:
    components = schema["components"]["schemas"]
    method = row["method"].lower()
    op = schema["paths"].get(row["path"], {}).get(method) if "schema" in row["source"] else None
    path_level = schema["paths"].get(row["path"], {}).get("parameters", []) if op else []

    frag = {
        "id": row["id"],
        "method": row["method"],
        "path": row["path"],
        "app": row["app"],
        "surface": row["surface"],
        "view": row["view"],
        "action": row["action"],
        "source_file": row["source_file"],
        "group": APP_GROUP.get(row["app"], "A_platform"),
        "folder": default_folder(row),
        "summary": "",
        "description": "",
        "auth": {"scheme": None, "permission": None, "scope": None, "notes": ""},
        "headers": [],
        "path_params": [],
        "query_params": [],
        "body": None,
        "success_status": None,
        "responses": [],
        "side_effects": [],
        "capture": [],
        "order": ACTION_ORDER.get(row["action"] or "", 60),
        "refused": row["refused"],
        "flag": row["flag"],
        "auto": False,
        "unresolved": [],
        "operation_id": row.get("operation_id"),
    }
    for name in re.findall(r"{(\w+)}", row["path"]):
        fixed = row["fixed_params"].get(name)
        ptype = row["path_params"].get(name, "str")
        entry = {"name": name, "type": ptype, "variable": variable_for(row["path"], row["surface"], name), "example": param_example(name, ptype, fixed), "description": ""}
        if isinstance(fixed, list):
            entry["choices"] = fixed
        frag["path_params"].append(entry)

    if op:
        frag["summary"] = (op.get("summary") or "").strip()
        frag["description"] = (op.get("description") or "").strip()
        security = op.get("security")
        if security is not None:
            names = [k for s in security for k in s]
            frag["auth"]["scheme"] = SECURITY_SCHEME.get(names[0], names[0]) if names else "none"
        for p in path_level + op.get("parameters", []):
            if p.get("in") == "query":
                ex = example_for(p.get("schema", {}), components, p["name"])
                if isinstance(ex, list):
                    ex = ex[0] if ex else ""
                frag["query_params"].append(
                    {
                        "key": p["name"],
                        "value": "" if ex is None else str(ex).lower() if isinstance(ex, bool) else str(ex),
                        "description": (p.get("description") or "").strip(),
                        "required": bool(p.get("required")),
                    }
                )
            elif p.get("in") == "header":
                frag["headers"].append({"key": p["name"], "value": "", "description": (p.get("description") or "").strip()})
            elif p.get("in") == "path":
                for pp in frag["path_params"]:
                    if pp["name"] == p["name"]:
                        pp["description"] = (p.get("description") or "").strip()
                        sch = resolve(p.get("schema", {}), components)
                        if sch.get("format") == "uuid":
                            pp["type"] = "uuid"
                        if sch.get("enum"):
                            pp["choices"] = sch["enum"]
                            pp["example"] = sch["enum"][0]
        rb = op.get("requestBody")
        if rb:
            content = rb["content"]
            ctype = "application/json" if "application/json" in content else next(iter(content))
            body_schema = content[ctype].get("schema", {})
            binaries = binary_fields(body_schema, components) if "multipart/form-data" in content else set()
            if binaries:
                ctype = "multipart/form-data"
                body_schema = content[ctype].get("schema", {})
                example = example_for(body_schema, components)
                formdata = []
                for key, value in (example or {}).items():
                    if key in binaries:
                        formdata.append({"key": key, "type": "file", "src": "", "description": "file upload"})
                    else:
                        formdata.append({"key": key, "type": "text", "value": value if isinstance(value, str) else json.dumps(value)})
                for key in binaries - set((example or {}).keys()):
                    formdata.append({"key": key, "type": "file", "src": "", "description": "file upload"})
                frag["body"] = {"mode": "formdata", "content_type": ctype, "formdata": formdata, "fields": field_list(body_schema, components), "required": bool(rb.get("required"))}
            else:
                frag["body"] = {"mode": "raw", "content_type": ctype, "raw": example_for(body_schema, components), "fields": field_list(body_schema, components), "required": bool(rb.get("required"))}
        for status, resp in sorted(op.get("responses", {}).items()):
            entry = {"status": int(status) if status.isdigit() else status, "description": (resp.get("description") or "").strip()}
            content = resp.get("content", {})
            if content and str(status).startswith("2"):
                ctype = next(iter(content))
                entry["content_type"] = ctype
                entry["example"] = example_for(content[ctype].get("schema", {}), components, request=False)
            frag["responses"].append(entry)
        success = [r["status"] for r in frag["responses"] if str(r["status"]).startswith("2")]
        frag["success_status"] = success[0] if success else None
        if row["action"] == "create" or (method == "post" and not row["path"].rstrip("/").endswith("}") and frag["success_status"] == 201):
            frag["capture"] = [{"var": collection_variable(row["path"], row["surface"]), "from": "uid"}]
    else:
        frag["unresolved"].append("not in OpenAPI schema: document from code")

    if row["refused"]:
        frag.update(refused_fields(row))
        frag.update({"folder": ["refused"], "body": {"mode": "raw", "content_type": "application/json", "raw": {}, "fields": [], "required": False}, "auto": True, "unresolved": []})
    if row["view"].endswith("AppendSlashView"):
        target = row["path"] + "/"
        frag.update(
            {
                "summary": f"301 redirect to the slashed old URL ({row['path'].removeprefix('/legacy')}/)",
                "description": (
                    "Legacy Django APPEND_SLASH behaviour (legacy/views/base.py AppendSlashView): any method on the slash-less old URL "
                    "answers 301 to the URL with the trailing slash (query string kept). 404 while the LEGACY_API_SHIM flag is off."
                ),
                "auth": {"scheme": "none", "permission": None, "scope": None, "notes": "Anonymous."},
                "success_status": 301,
                "responses": [{"status": 301, "description": f"Location: {target.removeprefix('/legacy')}"}, {"status": 404, "description": "LEGACY_API_SHIM off"}],
                "folder": ["Slash-less redirects (301)"],
                "order": 95,
                "auto": True,
                "unresolved": [],
            }
        )
    if frag["auth"]["scheme"] is None and row["surface"] in ("legacy", "iclock", "ops", "dev-media"):
        frag["unresolved"].append("auth scheme: determine from the view")
    return frag


def main():
    schema = json.loads((WORK / "openapi.v1.json").read_text())
    inventory = json.loads((WORK / "inventory.json").read_text())
    SEEDS.mkdir(exist_ok=True)
    groups: dict[str, list] = {}
    for row in inventory:
        frag = seed_row(row, schema)
        groups.setdefault(frag["group"], []).append(frag)
    for group, frags in sorted(groups.items()):
        (SEEDS / f"{group}.json").write_text(json.dumps(frags, indent=1, ensure_ascii=False) + "\n")
        auto = sum(f["auto"] for f in frags)
        print(f"{group:36} rows {len(frags):4}  auto {auto:4}  to read {len(frags) - auto:4}  apps {dict(Counter(f['app'] for f in frags))}")
    print("total", sum(len(v) for v in groups.values()))
    catalogue: dict[str, dict] = {}
    for frags in groups.values():
        for f in frags:
            for c in f["capture"]:
                catalogue.setdefault(c["var"], {"captured_by": [], "used_by": []})["captured_by"].append(f["id"])
            for p in f["path_params"]:
                catalogue.setdefault(p["variable"], {"captured_by": [], "used_by": []})["used_by"].append(f["id"])
    (WORK / "variables.json").write_text(json.dumps(dict(sorted(catalogue.items())), indent=1) + "\n")
    print("variables catalogue:", len(catalogue))


if __name__ == "__main__":
    import sys

    if "--sync-fragments" in sys.argv:
        sync_fragments()
    elif "--add-missing" in sys.argv:
        add_missing()
    else:
        main()
