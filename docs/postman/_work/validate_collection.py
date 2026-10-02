"""Phase 5.3: validate the collection against the official Postman Collection v2.1 JSON schema (host python3 + jsonschema).

    python3 docs/postman/_work/validate_collection.py

The schema (https://schema.getpostman.com/json/collection/v2.1.0/collection.json, draft-04) is vendored next to this
script as ``postman-collection-v2.1.schema.json`` so validation works offline. Also checks what the schema cannot but
Postman's importer does — every folder/request/response/script id is unique — plus the environment files' shape and
that no environment carries a non-empty secret. Exit 1 on any error.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import jsonschema

WORK = Path(__file__).resolve().parent
OUT = WORK.parent


def main():
    schema = json.loads((WORK / "postman-collection-v2.1.schema.json").read_text())
    collection = json.loads((OUT / "collection.postman_collection.json").read_text())
    validator = jsonschema.Draft4Validator(schema)
    errors = sorted(validator.iter_errors(collection), key=lambda e: list(e.absolute_path))
    for e in errors[:50]:
        print("SCHEMA ERROR", "/".join(map(str, e.absolute_path)), "->", e.message[:200])
    print(f"collection v2.1 schema errors: {len(errors)}")

    # Postman's importer also needs every id (folder, request, saved response, script) to be unique — the JSON schema
    # cannot express that, and newman ignores ids, so a duplicate passes everything else and still fails to import.
    seen, duplicates = {}, []

    def walk(items, trail):
        for item in items:
            name = " / ".join(trail + [item["name"]])
            entries = [(item.get("id"), name)]
            entries += [(r.get("id"), f"{name} :: response {r.get('name')}") for r in item.get("response", [])]
            entries += [(e.get("script", {}).get("id"), f"{name} :: {e.get('listen')} script") for e in item.get("event", [])]
            for ident, where in entries:
                if ident is None:
                    continue
                if ident in seen:
                    duplicates.append(f"{ident}: {seen[ident]}  AND  {where}")
                seen.setdefault(ident, where)
            if "item" in item:
                walk(item["item"], trail + [item["name"]])

    walk(collection["item"], [])
    for d in duplicates[:50]:
        print("DUPLICATE ID", d)
    print(f"duplicate ids: {len(duplicates)} (of {len(seen)})")
    errors = errors + duplicates

    # Postman's import scanner flags JWT-shaped strings as leaked secrets: examples must use plain placeholders.
    jwt_like = sorted(set(re.findall(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*", json.dumps(collection))))
    for token in jwt_like:
        print("JWT-SHAPED STRING", token[:40] + "...")
    print(f"JWT-shaped strings: {len(jwt_like)}")
    errors = errors + jwt_like

    env_errors = []
    for env_file in sorted(OUT.glob("*.postman_environment.json")):
        env = json.loads(env_file.read_text())
        if not {"id", "name", "values"} <= set(env):
            env_errors.append(f"{env_file.name}: missing id/name/values")
        for v in env.get("values", []):
            if not {"key", "value"} <= set(v):
                env_errors.append(f"{env_file.name}: bad value entry {v}")
            if v.get("type") == "secret" and v.get("value"):
                env_errors.append(f"{env_file.name}: secret {v['key']} is not empty")
    for e in env_errors:
        print("ENV ERROR", e)
    print(f"environment errors: {len(env_errors)}")
    sys.exit(1 if errors or env_errors else 0)


if __name__ == "__main__":
    main()
