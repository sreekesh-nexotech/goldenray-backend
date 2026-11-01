"""Merge the three endpoint sources into ``inventory.json`` (host python3, stdlib only).

    python3 docs/postman/_work/build_inventory.py

Sources (regenerate first, see README):
1. ``resolver.json``     — dump_urls.py, the live URL resolver (authoritative for what is routable);
2. ``openapi.v1.json``   — ``manage.py spectacular --api-version v1`` (drf-spectacular);
3. ``static_scan.json``  — static_scan.py (must report no problems).

Every (method, path) from any source is one row ``{id, method, path, view, app, surface, flag, source[]}``; ``id`` is
``"METHOD path"`` and stable. A schema operation absent from the resolver (or vice versa) is printed: rows the schema
cannot see (legacy, iclock, ops, dev-media — all ``schema = None`` or plain Django views) are expected, and so are
routed methods that can never succeed (``refused``: PUT on PATCH-only ViewSets → 403 unmapped action / 405).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

WORK = Path(__file__).resolve().parent
HTTP = ("get", "post", "put", "patch", "delete")
# Fallback per surface; the per-view flag detected by dump_urls.py (required_flag / require_flag / LegacyView) wins.
FLAGS = {"dev-media": "DEBUG and MEDIA_PUBLIC_BACKEND=local"}
# Surfaces whose views are deliberately outside the OpenAPI schema (documented in the coverage report).
SCHEMA_ABSENT_SURFACES = {"legacy", "iclock", "ops", "dev-media"}


def main():
    resolver = json.loads((WORK / "resolver.json").read_text())
    schema = json.loads((WORK / "openapi.v1.json").read_text())
    static = json.loads((WORK / "static_scan.json").read_text())
    if static["problems"]:
        sys.exit("static_scan.json has problems — fix them before building the inventory")

    rows = {}
    for r in resolver["rows"]:
        rid = f"{r['method']} {r['path']}"
        rows[rid] = {
            "id": rid,
            "method": r["method"],
            "path": r["path"],
            "view": r["view"],
            "action": r["action"],
            "app": r["app"],
            "surface": r["surface"],
            "flag": r["flag"] or FLAGS.get(r["surface"]),
            "source_file": r["source_file"],
            "path_params": r["path_params"],
            "fixed_params": r["fixed_params"],
            "url_name": r["url_name"],
            "regex": r["regex"],
            "pattern_index": r["pattern_index"],
            "refused": r["refused"],
            "source": ["resolver", "static"],  # static_scan verified every declared route resolves (no problems)
        }

    schema_only = []
    for path, item in schema["paths"].items():
        for method in item:
            if method not in HTTP:
                continue
            rid = f"{method.upper()} {path}"
            if rid in rows:
                rows[rid]["source"].append("schema")
                rows[rid]["operation_id"] = item[method].get("operationId")
            else:
                schema_only.append(rid)

    not_in_schema = [r for r in rows.values() if "schema" not in r["source"]]
    refused_absent = [r for r in not_in_schema if r["surface"] not in SCHEMA_ABSENT_SURFACES and r["refused"]]
    unexpected = [r["id"] for r in not_in_schema if r["surface"] not in SCHEMA_ABSENT_SURFACES and not r["refused"]]

    inventory = sorted(rows.values(), key=lambda r: (r["surface"], r["app"], r["path"], HTTP.index(r["method"].lower())))
    (WORK / "inventory.json").write_text(json.dumps(inventory, indent=1) + "\n")

    print(f"TOTAL endpoints (method, path): {len(inventory)}")
    print("by surface:", dict(sorted(Counter(r["surface"] for r in inventory).items())))
    print("by app:")
    for app, n in sorted(Counter(r["app"] for r in inventory).items(), key=lambda x: -x[1]):
        print(f"  {app:18} {n}")
    print(f"schema operations matched: {sum('schema' in r['source'] for r in inventory)}")
    print(f"not in schema (expected surfaces {sorted(SCHEMA_ABSENT_SURFACES)}): {len(not_in_schema) - len(unexpected) - len(refused_absent)}")
    print(f"not in schema, routed but always refused (403 unmapped / 405): {len(refused_absent)}")
    print(f"refused rows overall: {sum(1 for r in inventory if r['refused'])}")
    print(f"not in schema, UNEXPECTED: {len(unexpected)}")
    for rid in unexpected:
        print("   ", rid)
    print(f"in schema but NOT routable (phantom): {len(schema_only)}")
    for rid in schema_only:
        print("   ", rid)
    print(f"excluded automatic HEAD/OPTIONS handlers: {len(resolver['excluded_methods'])}")
    sys.exit(1 if schema_only or unexpected else 0)


if __name__ == "__main__":
    main()
