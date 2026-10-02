"""Phase 4: build the Postman collection + environments from the fragments (host python3, stdlib only, deterministic).

    python3 docs/postman/_work/build_collection.py

Reads ``fragments/*.json`` (one object per inventory row, see AGENT_BRIEF.md) and writes:

* ``docs/postman/collection.postman_collection.json``  — Postman Collection v2.1
* ``docs/postman/{local,staging,prod}.postman_environment.json`` — environments (secrets always empty)

Layout (run order top to bottom): 00 Auth → staff domains (platform → product master → configuration → sales → website
content → HR & devices) → public / agent / customer surfaces → iclock → legacy → refused methods → ops → 98 Cleanup
(every staff DELETE, reverse dependency order) → 99 Sign out. Inside a resource folder requests are sorted by the
fragment ``order`` (list, create, retrieve, update, actions …).

Auth is set once at collection level (bearer ``{{access_token}}``); requests on other schemes override it (agent token
bearer, or no auth). Login/refresh store tokens; creates store the new ``uid``; every request asserts its status code and
JSON shape. Ids (``_postman_id``) are uuid5 of the endpoint id, so a rebuild with the same fragments is byte-identical.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from collections import OrderedDict
from pathlib import Path

WORK = Path(__file__).resolve().parent
OUT_DIR = WORK.parent
NS = uuid.UUID("6f1c3a52-1d7e-4a0e-9a57-5d3b8f0c2e11")
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
PLACEHOLDER_UUID = "00000000-0000-4000-8000-000000000000"

DOMAINS = [
    ("01 Platform", ["accounts", "core", "audit", "media", "documents", "company"]),
    ("02 Product master", ["catalog", "procurement", "inventory", "pricing"]),
    ("03 Configuration", ["engineering", "bom", "packs"]),
    ("04 Sales", ["customers", "leads", "quotations", "agreements", "site_inspections", "projects"]),
    ("05 Website content", ["reference", "calculators", "emi", "sitepages", "blog", "faqs", "seo", "careers"]),
    ("06 HR, attendance & devices", ["hr", "attendance", "devices"]),
]
APP_DOMAIN = {app: (i, name) for i, (name, apps) in enumerate(DOMAINS) for app in apps}
APP_INDEX = {app: j for _, apps in DOMAINS for j, app in enumerate(apps)}
APP_LABEL = {
    "accounts": "Accounts",
    "core": "Core",
    "audit": "Audit",
    "media": "Media",
    "documents": "Documents",
    "company": "Company",
    "catalog": "Catalog",
    "procurement": "Procurement",
    "inventory": "Inventory",
    "pricing": "Pricing",
    "engineering": "Engineering",
    "bom": "BOM",
    "packs": "Packs",
    "customers": "Customers",
    "leads": "Leads",
    "quotations": "Quotations",
    "agreements": "Agreements",
    "site_inspections": "Site inspections",
    "projects": "Projects",
    "reference": "Reference data",
    "calculators": "Calculators",
    "emi": "EMI",
    "sitepages": "Site pages",
    "blog": "Content (blog CMS)",
    "faqs": "FAQs",
    "seo": "SEO",
    "careers": "Careers",
    "hr": "HR",
    "attendance": "Attendance",
    "devices": "Devices",
    "legacy": "Legacy",
    "django": "Django",
}
SURFACE_FOLDERS = {
    "public": "07 Public website API (/api/public, anonymous)",
    "agent": "08 Office agent API (/api/agent, service token)",
    "customer": "09 Customer links (/api/customer, signed token + OTP)",
    "iclock": "10 iclock terminals (/iclock, flag ADMS_RECEIVER)",
    "legacy": "11 Legacy shim (/legacy, flag LEGACY_API_SHIM)",
}
REFUSED_FOLDER = "12 Refused methods (routed, always 403/405)"
OPS_FOLDER = "13 Ops & docs"
CLEANUP_FOLDER = "98 Cleanup (staff deletes, reverse dependency order)"
SIGNOUT_FOLDER = "99 Sign out"
AUTH_FOLDER = "00 Auth"
STATUS_TEXT = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    410: "Gone",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}
SURFACE_URL = [
    ("/api/public/v1/", "/api/public/{{api_version}}/"),
    ("/api/agent/v1/", "/api/agent/{{api_version}}/"),
    ("/api/customer/v1/", "/api/customer/{{api_version}}/"),
    ("/api/schema/v1/", "/api/schema/{{api_version}}/"),
    ("/api/v1/", "/api/{{api_version}}/"),
]

STORE_FN = (
    "const __store = (k, v) => { if (v === undefined || v === null || v === '') return; pm.collectionVariables.set(k, String(v)); "
    "if (pm.environment && pm.environment.name) pm.environment.set(k, String(v)); };"
)
GET_FN = "const __get = (obj, path) => path.split('.').reduce((o, k) => (o === undefined || o === null) ? undefined : o[/^\\d+$/.test(k) ? Number(k) : k], obj);"


def pid(*parts: str) -> str:
    return str(uuid.uuid5(NS, "|".join(parts)))


def load_fragments() -> list[dict]:
    frags = []
    for file in sorted((WORK / "fragments").glob("*.json")):
        frags += json.loads(file.read_text())
    return frags


def var_of(frag: dict, name: str) -> str:
    for p in frag["path_params"]:
        if p["name"] == name:
            return p["variable"]
    return name


def url_for(frag: dict, path_values: dict | None = None, query_params: list | None = None) -> dict:
    path_values = path_values or {}
    path = re.sub(r"{(\w+)}", lambda m: path_values.get(m.group(1)) or "{{" + var_of(frag, m.group(1)) + "}}", frag["path"])
    for old, new in SURFACE_URL:
        if path.startswith(old):
            path = new + path[len(old) :]
            break
    segments = path.lstrip("/").split("/")
    query = []
    for q in query_params if query_params is not None else frag["query_params"]:
        value = "" if q.get("value") is None else str(q.get("value"))
        # a literal "+" in a query string is decoded as a space by Django's QueryDict: send it percent-encoded
        entry = {"key": q["key"], "value": value.replace("+", "%2B")}
        if q.get("description"):
            entry["description"] = q["description"]
        if not q.get("required"):
            entry["disabled"] = True
        query.append(entry)
    raw = "{{base_url}}" + path
    enabled = [q for q in query if not q.get("disabled")]
    if enabled:
        raw += "?" + "&".join(f"{q['key']}={q['value']}" for q in enabled)
    url = {"raw": raw, "host": ["{{base_url}}"], "path": segments}
    if query:
        url["query"] = query
    return url


def body_for(body: dict | None) -> dict | None:
    if not body:
        return None
    if body.get("mode") == "formdata":
        items = []
        for f in body.get("formdata") or []:
            item = {"key": f["key"], "type": f.get("type", "text")}
            if item["type"] == "file":
                item["src"] = f.get("src") or []
            else:
                value = f.get("value", "")
                item["value"] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            if f.get("description"):
                item["description"] = f["description"]
            items.append(item)
        return {"mode": "formdata", "formdata": items}
    if body.get("mode") == "urlencoded":
        return {"mode": "urlencoded", "urlencoded": [{"key": k, "value": str(v)} for k, v in (body.get("raw") or {}).items()]}
    raw = body.get("raw")
    ctype = body.get("content_type") or "application/json"
    if isinstance(raw, str) and "json" not in ctype:
        return {"mode": "raw", "raw": raw, "options": {"raw": {"language": "text"}}}
    return {"mode": "raw", "raw": json.dumps(raw if raw is not None else {}, indent=2, ensure_ascii=False), "options": {"raw": {"language": "json"}}}


def auth_for(frag: dict) -> dict | None:
    scheme = frag["auth"]["scheme"]
    if scheme == "staff_jwt":
        return None  # inherit the collection's bearer {{access_token}}
    if scheme == "agent_token":
        return {"type": "bearer", "bearer": [{"key": "token", "value": "{{agent_token}}", "type": "string"}]}
    return {"type": "noauth"}


def expected_statuses(frag: dict) -> list[int]:
    if frag.get("refused"):
        status = int(frag["refused"][:3])
        return [403] if status == 403 else [status, 403]  # a 405 refusal is 403 for callers without the permission
    status = frag.get("success_status")
    if isinstance(status, list):
        return [int(s) for s in status]
    return [int(status)] if status else [200]


def success_example(frag: dict):
    for r in frag["responses"]:
        if str(r.get("status")).startswith("2") and "example" in r:
            return r["example"]
    return None


def test_script(frag: dict, captures: list | None = None) -> list[str]:
    statuses = expected_statuses(frag)
    lines = [STORE_FN, GET_FN]
    if len(statuses) == 1:
        lines.append(f"pm.test('status is {statuses[0]}', () => pm.response.to.have.status({statuses[0]}));")
    else:
        lines.append(f"pm.test('status in {statuses}', () => pm.expect(pm.response.code).to.be.oneOf({json.dumps(statuses)}));")
    example = success_example(frag)
    ok = statuses[0]
    lines.append("const __isJson = (pm.response.headers.get('Content-Type') || '').includes('json');")
    lines.append(f"const __ok = pm.response.code === {ok} && __isJson;")
    if isinstance(example, dict) and example:
        if {"results", "count"} <= set(example):
            lines.append(
                "if (__ok) pm.test('paginated JSON (count, results[])', () => { const j = pm.response.json(); pm.expect(j).to.have.property('count'); pm.expect(j.results).to.be.an('array'); });"
            )
        else:
            keys = [k for k in list(example)[:4]]
            lines.append(f"if (__ok) pm.test('JSON has {', '.join(keys)}', () => {{ const j = pm.response.json(); {' '.join(f'pm.expect(j).to.have.property({json.dumps(k)});' for k in keys)} }});")
    elif isinstance(example, list):
        lines.append("if (__ok) pm.test('JSON array', () => pm.expect(pm.response.json()).to.be.an('array'));")
    if 300 <= ok < 400:
        lines.append("pm.test('redirects (Location set)', () => pm.expect(pm.response.headers.get('Location') || '').to.not.equal(''));")
    for cap in captures if captures is not None else frag["capture"]:
        if cap.get("regex"):
            # value = first capture group of ``regex`` applied to the field (e.g. a token inside a returned link)
            lines.append(
                f"if (__ok) {{ const __s = __get(pm.response.json(), {json.dumps(cap['from'])}); "
                f"const __m = __s ? String(__s).match(new RegExp({json.dumps(cap['regex'])})) : null; if (__m) __store({json.dumps(cap['var'])}, __m[1]); }}"
            )
        else:
            lines.append(f"if (__ok) {{ const __v = __get(pm.response.json(), {json.dumps(cap['from'])}); __store({json.dumps(cap['var'])}, __v); }}")
    return lines


def description_for(frag: dict) -> str:
    out = []
    if frag.get("description"):
        out.append(frag["description"])
    auth = frag["auth"]
    bits = [f"**Auth:** `{auth['scheme']}`"]
    if auth.get("permission"):
        bits.append(f"permission `{auth['permission']}`")
    if auth.get("scope"):
        bits.append(f"scope: {auth['scope']}")
    out.append(" · ".join(bits) + (f" — {auth['notes']}" if auth.get("notes") else ""))
    if frag.get("flag"):
        out.append(f"**Flag:** `{frag['flag']}` must be enabled (404 otherwise).")
    if frag.get("refused"):
        out.append(f"**Refused for every caller:** {frag['refused']}.")
    if frag["path_params"]:
        out.append(
            "**Path params:** "
            + "; ".join(
                f"`{p['name']}` ({p.get('type', 'str')}) ← `{{{{{p['variable']}}}}}`"
                + (f" — {p['description']}" if p.get("description") else "")
                + (f" choices: {', '.join(map(str, p['choices']))}" if p.get("choices") else "")
                for p in frag["path_params"]
            )
        )
    fields = (frag.get("body") or {}).get("fields") or []
    if fields:
        out.append(
            "**Body fields:** "
            + "; ".join(f"`{f['name']}`{'*' if f.get('required') else ''} {f.get('type', '')}" + (f" [{', '.join(map(str, f['choices']))}]" if f.get("choices") else "") for f in fields)
            + "  (* required)"
        )
    if frag.get("side_effects"):
        out.append("**Side effects:** " + "; ".join(frag["side_effects"]))
    statuses = [f"{r['status']}" + (f" {r['description']}" if r.get("description") else "") for r in frag["responses"]]
    if statuses:
        out.append("**Responses:** " + "; ".join(statuses))
    if frag.get("unresolved"):
        out.append("**Needs review:** " + "; ".join(frag["unresolved"]))
    out.append(f"_Source: `{frag['view']}`" + (f" ({frag['source_file']})" if frag.get("source_file") else "") + f" · id `{frag['id']}`_")
    return "\n\n".join(out)


def request_item(frag: dict, name: str | None = None, path_values=None, query_params=None, body=..., captures=None, key_suffix: str = "", responses=None) -> dict:
    headers = []
    for h in frag.get("headers", []):
        header = {"key": h["key"], "value": h.get("value", "")}
        if h.get("description"):
            header["description"] = h["description"]
        if h.get("disabled") or h.get("required") is False or h.get("optional"):
            header["disabled"] = True
        headers.append(header)
    body_def = frag.get("body") if body is ... else body
    built_body = body_for(body_def)
    if built_body and built_body["mode"] == "raw":
        ctype = (body_def or {}).get("content_type") or "application/json"
        headers.append({"key": "Content-Type", "value": ctype})
    request = {"method": frag["method"], "header": headers, "url": url_for(frag, path_values, query_params), "description": description_for(frag)}
    if built_body:
        request["body"] = built_body
    auth = auth_for(frag)
    if auth:
        request["auth"] = auth
    item = {
        "id": pid(frag["id"], key_suffix),
        **({"protocolProfileBehavior": {"followRedirects": False}} if any(300 <= st < 400 for st in expected_statuses(frag)) else {}),
        "name": name or f"{frag['method']} {frag['path']} - {frag['summary']}",
        "event": [{"listen": "test", "script": {"id": pid(frag["id"], key_suffix, "test"), "type": "text/javascript", "exec": test_script(frag, captures)}}],
        "request": request,
        "response": [],
    }
    for index, r in enumerate(responses if responses is not None else frag["responses"]):
        code = r.get("status")
        if not isinstance(code, int):
            continue
        example = r.get("example")
        if example is None and not str(code).startswith("2"):
            continue
        is_text = isinstance(example, str)
        item["response"].append(
            {
                "id": pid(frag["id"], key_suffix, "resp", str(index), str(code)),  # two examples may share a code
                "name": f"{code} {r.get('description') or STATUS_TEXT.get(code, '')}".strip()[:120],
                "originalRequest": {k: v for k, v in request.items() if k != "description"},
                "status": STATUS_TEXT.get(code, ""),
                "code": code,
                "_postman_previewlanguage": "text" if is_text else "json",
                "header": [{"key": "Content-Type", "value": r.get("content_type") or ("text/plain" if is_text else "application/json")}],
                "body": example if is_text else ("" if example is None else json.dumps(example, indent=2, ensure_ascii=False)),
            }
        )
    return item


def is_auth_row(f: dict) -> bool:
    return f["app"] == "accounts" and f["path"].startswith("/api/v1/auth/")


def is_signout(f: dict) -> bool:
    return f["path"] == "/api/v1/auth/logout/"


def placement(f: dict) -> tuple[list[str], tuple]:
    """(folder path, sort key) for a fragment."""
    app_label = APP_LABEL.get(f["app"], f["app"])
    resource = [str(x) for x in f["folder"]] or ["General"]
    if is_signout(f):
        return [SIGNOUT_FOLDER], (99,)
    if f.get("refused"):
        return [REFUSED_FOLDER, app_label], (12,)
    if f["surface"] in ("ops", "dev-media"):
        return [OPS_FOLDER], (13,)
    if is_auth_row(f):
        return [AUTH_FOLDER], (0,)
    if f["surface"] in SURFACE_FOLDERS:
        if f["surface"] in ("iclock",):
            return [SURFACE_FOLDERS[f["surface"]]], (10,)
        if f["surface"] == "legacy":
            return [SURFACE_FOLDERS["legacy"], *resource], (11,)
        return [SURFACE_FOLDERS[f["surface"]], app_label, *resource], (7 + list(SURFACE_FOLDERS).index(f["surface"]),)
    di, domain = APP_DOMAIN.get(f["app"], (0, DOMAINS[0][0]))
    if f["method"] == "DELETE":
        return [CLEANUP_FOLDER, f"{domain} / {app_label}", *resource], (98,)
    return [domain, app_label, *resource], (1 + di,)


def folder_sort_key(name: str) -> tuple:
    m = re.match(r"^(\d\d) ", name)
    return (int(m.group(1)) if m else 50, name)


def build():
    frags = load_fragments()
    placed = []
    for f in frags:
        path, key = placement(f)
        placed.append((path, key, f))

    # Folder ordering: numbered top-level folders by number; app folders by dependency order (reversed in Cleanup);
    # resource folders by the smallest fragment order then name; requests by (order, path, method).
    def app_rank(label: str) -> int:
        for app, lab in APP_LABEL.items():
            if lab == label or label.endswith("/ " + lab):
                return APP_DOMAIN.get(app, (0, ""))[0] * 100 + APP_INDEX.get(app, 0)
        return 10_000

    root: dict = {"__items__": [], "__children__": OrderedDict()}
    for path, _, f in placed:
        node = root
        for part in path:
            node = node["__children__"].setdefault(part, {"__items__": [], "__children__": OrderedDict()})
        node["__items__"].append(f)

    method_rank = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}

    def min_order(node) -> int:
        orders = [f.get("order", 60) for f in node["__items__"]] + [min_order(c) for c in node["__children__"].values()]
        return min(orders) if orders else 999

    def render(name: str, node: dict, depth: int, top: str, trail: tuple = ()) -> dict:
        children = list(node["__children__"].items())
        if depth == 0:
            children.sort(key=lambda kv: folder_sort_key(kv[0]))
        elif depth == 1 and top == CLEANUP_FOLDER:
            children.sort(key=lambda kv: (-app_rank(kv[0]), kv[0]))
        elif depth == 1 and top not in (AUTH_FOLDER, OPS_FOLDER):
            children.sort(key=lambda kv: (app_rank(kv[0]), kv[0]))
        else:
            children.sort(key=lambda kv: (min_order(kv[1]), kv[0]))
        items = sorted(node["__items__"], key=lambda f: (f.get("order", 60), f["path"], method_rank.get(f["method"], 9)))
        out = []
        for f in items:
            out.append(request_item(f))
            for i, variant in enumerate(f.get("variants") or []):
                body = variant.get("body", f.get("body"))
                out.append(
                    request_item(
                        f,
                        name=f"{f['method']} {f['path']} - {variant['name']}",
                        path_values=variant.get("path_values"),
                        query_params=variant.get("query_params", f["query_params"]),
                        body=body,
                        captures=variant.get("capture", []),
                        key_suffix=f"variant{i}",
                        responses=(
                            [
                                {
                                    "status": expected_statuses(f)[0],
                                    "description": variant["name"],
                                    "example": variant["response"],
                                    "content_type": "text/plain" if isinstance(variant["response"], str) else "application/json",
                                }
                            ]
                            if "response" in variant
                            else None
                        ),
                    )
                )
        rendered_children = [render(child_name, child, depth + 1, top or child_name, trail + (child_name,)) for child_name, child in children]
        # In Cleanup, nested resources (e.g. Templates › Slots) are deleted before their parent's own DELETE.
        out = rendered_children + out if top == CLEANUP_FOLDER else out + rendered_children
        # folder ids hash the full folder path: same-named folders (e.g. "Notes") live under different parents
        return {"id": pid("folder", *trail), "name": name, "item": out} if depth else {"item": out}

    rendered = render("", root, 0, "")

    # Collection variables: every {{var}} used anywhere, with defaults from path-param examples.
    defaults = OrderedDict(
        [
            ("base_url", DEFAULT_BASE_URL),
            ("api_version", "v1"),
            ("staff_email", ""),
            ("staff_password", ""),
            ("access_token", ""),
            ("refresh_token", ""),
            ("agent_token", ""),
        ]
    )
    for f in frags:  # path-param examples first: a variable's initial value must be usable before any capture runs
        for p in f["path_params"]:
            example = "" if p.get("example") is None else str(p["example"])
            if "{{" in example:  # never default a variable to a template of itself
                example = ""
            if example and not defaults.get(p["variable"]):
                defaults[p["variable"]] = example
            defaults.setdefault(p["variable"], "")
    for f in frags:
        for p in f["path_params"]:
            if not defaults.get(p["variable"]) and (p["variable"].endswith("_uid") or p.get("type") == "uuid"):
                defaults[p["variable"]] = PLACEHOLDER_UUID
    conflicts = []
    for f in frags:  # a choice-constrained param must get a valid default, or Django routes the request elsewhere
        for p in f["path_params"]:
            if p.get("choices") and defaults.get(p["variable"]) not in [str(c) for c in p["choices"]]:
                conflicts.append(f"{f['id']}: {{{{{p['variable']}}}}} = {defaults.get(p['variable'])!r} not in {p['choices']}")
    if conflicts:
        sys.exit("variable defaults violate path-param choices:\n  " + "\n  ".join(conflicts))
    for f in frags:
        for c in f["capture"] + [c for v in f.get("variants") or [] for c in v.get("capture") or []]:
            defaults.setdefault(c["var"], "")
    text = json.dumps(rendered, ensure_ascii=False)
    used = sorted(set(re.findall(r"{{([A-Za-z0-9_\-.]+)}}", text)))
    builtin = {"$guid", "$timestamp", "$randomInt", "$isoTimestamp", "$randomUUID"}
    undefined = []
    for name in used:
        if name not in defaults and name not in builtin and not name.startswith("$"):
            defaults[name] = PLACEHOLDER_UUID if name.endswith("_uid") else ""
            undefined.append(name)
    variables = [{"key": k, "value": v, "type": "string"} for k, v in defaults.items()]

    collection = {
        "info": {
            "_postman_id": pid("collection"),
            "name": "Flarize Platform API",
            "description": (
                "Every HTTP endpoint of the Flarize backend (generated by docs/postman/_work/build_collection.py — do not edit by hand; "
                "see docs/postman/README.md).\n\nSurfaces: `/api/{{api_version}}/` staff (JWT), `/api/public/…` website (anonymous), "
                "`/api/agent/…` office agents (service token), `/api/customer/…` customer signed links, `/iclock/…` terminals "
                "(flag ADMS_RECEIVER), `/legacy/…` old website contracts (flag LEGACY_API_SHIM), ops.\n\n"
                "Run order: 00 Auth (log in) → domain folders (creates store uids in variables) → 98 Cleanup → 99 Sign out. "
                "Errors use the envelope `{code, message, errors, error_codes}`."
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{access_token}}", "type": "string"}]},
        "variable": variables,
        "item": rendered["item"],
    }
    (OUT_DIR / "collection.postman_collection.json").write_text(json.dumps(collection, indent=1, ensure_ascii=False) + "\n")

    for env, base in (("local", DEFAULT_BASE_URL), ("staging", "https://staging-api.example.invalid"), ("prod", "https://api.example.invalid")):
        values = [
            {"key": "base_url", "value": base, "type": "default", "enabled": True},
            {"key": "api_version", "value": "v1", "type": "default", "enabled": True},
            {"key": "staff_email", "value": "", "type": "default", "enabled": True},
            {"key": "staff_password", "value": "", "type": "secret", "enabled": True},
            {"key": "access_token", "value": "", "type": "secret", "enabled": True},
            {"key": "refresh_token", "value": "", "type": "secret", "enabled": True},
            {"key": "agent_token", "value": "", "type": "secret", "enabled": True},
            {"key": "staff_new_password", "value": "", "type": "secret", "enabled": True},
            {"key": "password_reset_token", "value": "", "type": "secret", "enabled": True},
        ]
        environment = {"id": pid("environment", env), "name": f"Flarize {env}", "values": values, "_postman_variable_scope": "environment"}
        (OUT_DIR / f"{env}.postman_environment.json").write_text(json.dumps(environment, indent=1) + "\n")

    count = text.count('"request":')
    print(f"fragments: {len(frags)}  requests: {count}  variables: {len(variables)}")
    if undefined:
        print(f"variables referenced but not produced by any path param/capture ({len(undefined)}), defaulted:", ", ".join(undefined))
    return collection


if __name__ == "__main__":
    if not list((WORK / "fragments").glob("*.json")):
        sys.exit("no fragments yet")
    build()
