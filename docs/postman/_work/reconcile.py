"""Phase 3: diff ``inventory.json`` against every ``fragments/*.json`` (host python3, stdlib only).

    python3 docs/postman/_work/reconcile.py                 # all groups; exit 1 on any problem
    python3 docs/postman/_work/reconcile.py --group H_blog  # one group (subagent self-check)

Reports MISSING (inventory row without a fragment), PHANTOM (fragment id not in the inventory), MISMATCH (method/path of
a fragment differs from its id's inventory row), DUPLICATE ids, and MALFORMED fragments (required keys, auth scheme,
success status, a body example on POST/PUT/PATCH unless ``no_body`` is declared, a variable on every path param,
capture variables named). ``unresolved`` notes are printed as warnings, not failures.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

WORK = Path(__file__).resolve().parent
REQUIRED = ("id", "method", "path", "app", "surface", "group", "folder", "summary", "auth", "path_params", "query_params", "body", "success_status", "responses", "side_effects", "capture", "order")
SCHEMES = {"staff_jwt", "none", "agent_token", "customer_link", "device_token", "signed_link", "refresh_cookie"}


def load_fragments(group: str | None):
    frags = []
    for file in sorted((WORK / "fragments").glob("*.json")):
        if group and file.stem != group:
            continue
        data = json.loads(file.read_text())
        for f in data:
            f["_file"] = file.name
        frags += data
    return frags


def malformed(f: dict) -> list[str]:
    issues = [f"missing key '{k}'" for k in REQUIRED if k not in f]
    if issues:
        return issues
    if not f["summary"]:
        issues.append("empty summary")
    scheme = (f.get("auth") or {}).get("scheme")
    if scheme not in SCHEMES:
        issues.append(f"auth.scheme {scheme!r} not in {sorted(SCHEMES)}")
    if not f.get("refused") and not f["success_status"]:
        issues.append("no success_status")
    if f["method"] in ("POST", "PUT", "PATCH") and not f.get("refused"):
        body = f["body"]
        if body is None and not f.get("no_body"):
            issues.append("POST/PUT/PATCH without body (set no_body: true if the endpoint takes none)")
        elif body is not None:
            if body.get("mode") == "raw" and body.get("raw") is None:
                issues.append("raw body without example")
            if body.get("mode") == "formdata" and not body.get("formdata"):
                issues.append("formdata body without fields")
    names = re.findall(r"{(\w+)}", f["path"])
    declared = {p.get("name"): p for p in f["path_params"]}
    for n in names:
        if n not in declared:
            issues.append(f"path param {n} not declared")
        elif not declared[n].get("variable"):
            issues.append(f"path param {n} has no variable")
    for c in f["capture"]:
        if not c.get("var") or not c.get("from"):
            issues.append(f"capture entry incomplete: {c}")
    if not isinstance(f["folder"], list) or not f["folder"]:
        issues.append("folder must be a non-empty list")
    return issues


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group")
    args = parser.parse_args()

    inventory = {r["id"]: r for r in json.loads((WORK / "inventory.json").read_text())}
    if args.group:
        seed_ids = {f["id"] for f in json.loads((WORK / "seeds" / f"{args.group}.json").read_text())}
        inventory = {k: v for k, v in inventory.items() if k in seed_ids}
    frags = load_fragments(args.group)

    counts = Counter(f["id"] for f in frags)
    duplicates = sorted(k for k, n in counts.items() if n > 1)
    by_id = {f["id"]: f for f in frags}
    missing = sorted(set(inventory) - set(by_id))
    phantom = sorted(set(by_id) - set(inventory))
    mismatch = sorted(f"{fid}: fragment has {f['method']} {f['path']}" for fid, f in by_id.items() if fid in inventory and f"{f['method']} {f['path']}" != fid)
    bad = {fid: malformed(f) for fid, f in by_id.items()}
    for fid, f in by_id.items():
        row = inventory.get(fid)
        if row:
            drift = [k for k in ("refused", "flag", "view") if f.get(k) != row.get(k)]
            if drift:
                bad.setdefault(fid, []).append(f"out of sync with inventory on {drift} (run seed_fragments.py --sync-fragments)")
    bad = {k: v for k, v in bad.items() if v}
    unresolved = {fid: f["unresolved"] for fid, f in by_id.items() if f.get("unresolved")}

    print(f"inventory rows: {len(inventory)}  fragments: {len(frags)}  unique ids: {len(by_id)}")
    for title, items in (("MISSING", missing), ("PHANTOM", phantom), ("MISMATCH", mismatch), ("DUPLICATE", duplicates)):
        print(f"{title}: {len(items)}")
        for item in items:
            print("   ", item)
    print(f"MALFORMED: {len(bad)}")
    for fid, issues in sorted(bad.items()):
        print("   ", fid, "->", "; ".join(issues))
    print(f"warnings (unresolved notes): {len(unresolved)}")
    for fid, notes in sorted(unresolved.items()):
        print("   ", fid, "->", "; ".join(notes))
    failed = missing or phantom or mismatch or duplicates or bad
    print("RESULT:", "FAIL" if failed else "OK")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
