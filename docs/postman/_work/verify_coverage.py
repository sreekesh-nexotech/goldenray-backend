"""Phase 5: prove the FINAL collection covers every inventory endpoint (host python3, stdlib only).

    python3 docs/postman/_work/verify_coverage.py   # exit 0 only when 0 endpoints are missing

Parses ``docs/postman/collection.postman_collection.json`` independently of the fragments: walks every request, takes its
method and URL path (``{{base_url}}`` dropped, ``{{api_version}}`` → ``v1``, query string dropped, trailing slash kept)
and matches it against ``inventory.json``. An inventory ``{param}`` segment matches any one segment; a Postman
``{{variable}}`` / ``:param`` segment only matches a ``{param}`` segment. A request counts for the most specific
inventory row it matches (most literal segments).

Then the request is resolved the way Django does: its ``{{variables}}`` are replaced by their collection default values
and the concrete path is matched against every pattern regex in urlconf order — the FIRST match is where it really goes.
If that is a different endpoint than the pattern match (e.g. a variable default that a choice-constrained param rejects,
so the request falls through to a catch-all), it is a MISROUTE failure. Requests whose variables have no usable default
(filled only at run time by a capture) are counted as "unresolvable" and keep the pattern match.

Reports MISSING rows, EXTRA requests (match nothing), MISROUTE requests, per-app coverage, and writes ``coverage.json``.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

WORK = Path(__file__).resolve().parent
COLLECTION = WORK.parent / "collection.postman_collection.json"


def walk(items, trail=()):
    for item in items:
        if "item" in item:
            yield from walk(item["item"], trail + (item["name"],))
        elif "request" in item:
            yield trail, item


def request_path(request: dict) -> str:
    url = request["url"]
    raw = url["raw"] if isinstance(url, dict) else url
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    raw = raw.replace("{{base_url}}", "").replace("{{api_version}}", "v1")
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def segments(path: str) -> list[str]:
    return path.split("/")[1:]  # keeps a trailing "" for a trailing slash


def is_var(seg: str) -> bool:
    return bool(re.fullmatch(r"{{[^{}]+}}|:\w+", seg))


def match_score(inv_segs: list[str], req_segs: list[str]) -> int | None:
    if len(inv_segs) != len(req_segs):
        return None
    score = 0
    for inv, req in zip(inv_segs, req_segs):
        if re.fullmatch(r"{\w+}", inv):
            if req == "":
                return None
            continue
        if is_var(req) or inv != req:
            return None
        score += 1
    return score


def concrete(path: str, variables: dict) -> str | None:
    """The path with every ``{{var}}`` replaced by its default, or None when a default is missing/empty/templated."""
    out = path
    for name in re.findall(r"{{([^{}]+)}}", path):
        value = variables.get(name, "")
        if not value or "{{" in value:
            return None
        out = out.replace("{{" + name + "}}", value)
    return out


def django_resolve(path: str, patterns: list) -> list | None:
    """Rows of the first pattern (urlconf order) whose full regex matches all of ``path`` — Django's resolution rule
    (each resolver level matches a prefix; the leaf must consume the rest; ``$``/``\\Z`` anchors already end the regex)."""
    target = path.lstrip("/")
    for regex, rows in patterns:
        m = re.match(regex, target)
        if m and m.end() == len(target):
            return rows
    return None


def main():
    inventory = json.loads((WORK / "inventory.json").read_text())
    collection = json.loads(COLLECTION.read_text())
    variables = {v["key"]: v.get("value", "") for v in collection.get("variable", [])}
    by_pattern = defaultdict(list)
    for row in inventory:
        by_pattern[(row["pattern_index"], row["regex"])].append(row)
    patterns = [(regex, rows) for (_, regex), rows in sorted(by_pattern.items())]
    by_method = defaultdict(list)
    for row in inventory:
        by_method[row["method"]].append((row, segments(row["path"])))

    covered = defaultdict(list)
    extras, misroutes, unresolvable, total = [], [], [], 0
    for trail, item in walk(collection["item"]):
        total += 1
        method = item["request"]["method"].upper()
        path = request_path(item["request"])
        req_segs = segments(path)
        best, best_score = None, -1
        for row, inv_segs in by_method.get(method, []):
            score = match_score(inv_segs, req_segs)
            if score is not None and score > best_score:
                best, best_score = row, score
        label = f"{method} {path}  [{' / '.join(trail)} :: {item['name']}]"
        if best is None:
            extras.append(label)
            continue
        resolved_path = concrete(path, variables)
        if resolved_path is None:
            unresolvable.append(label)
            covered[best["id"]].append(item["name"])
            continue
        rows = django_resolve(resolved_path, patterns)
        real = next((r for r in rows or [] if r["method"] == method), None)
        if real is None or real["id"] != best["id"]:
            where = real["id"] if real else ("no route" if rows is None else f"route {rows[0]['path']} without {method}")
            misroutes.append(f"{label} -> concrete {resolved_path} resolves to {where}, not {best['id']}")
            if real is not None:
                covered[real["id"]].append(item["name"])
            continue
        covered[best["id"]].append(item["name"])

    missing = [r for r in inventory if r["id"] not in covered]
    per_app = defaultdict(lambda: [0, 0])
    for r in inventory:
        per_app[r["app"]][0] += 1
        per_app[r["app"]][1] += r["id"] in covered
    per_surface = Counter(r["surface"] for r in inventory)

    report = {
        "inventory_total": len(inventory),
        "collection_requests": total,
        "covered": len(covered),
        "missing": [r["id"] for r in missing],
        "extras": extras,
        "misroutes": misroutes,
        "unresolvable_by_default_values": len(unresolvable),
        "per_app": {app: {"inventory": n, "covered": c} for app, (n, c) in sorted(per_app.items())},
        "per_surface": dict(sorted(per_surface.items())),
        "multi_covered": {k: v for k, v in covered.items() if len(v) > 1},
    }
    (WORK / "coverage.json").write_text(json.dumps(report, indent=1) + "\n")

    print(f"inventory endpoints: {len(inventory)}  collection requests: {total}  covered: {len(covered)}")
    print(f"{'app':18} inventory covered")
    for app, (n, c) in sorted(per_app.items()):
        print(f"{app:18} {n:9} {c:7}{'' if n == c else '  <-- MISSING'}")
    print(f"MISSING: {len(missing)}")
    for r in missing:
        print("   ", r["id"])
    print(f"EXTRA requests matching no inventory row: {len(extras)}")
    for e in extras:
        print("   ", e)
    print(f"MISROUTE (Django resolves the concrete URL elsewhere): {len(misroutes)}")
    for m in misroutes:
        print("   ", m)
    print(f"resolved like Django: {total - len(extras) - len(unresolvable)}  pattern-only (a variable has no default until captured): {len(unresolvable)}")
    sys.exit(1 if missing or extras or misroutes else 0)


if __name__ == "__main__":
    main()
