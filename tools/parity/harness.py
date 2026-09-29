#!/usr/bin/env python3
"""Parity harness (PLAN §6.3): replay a request corpus against the legacy servers and the new API's ``/legacy/`` shim.

Standalone (standard library only). Every corpus line is one request::

    {"id": "reference/device-types", "group": "reference", "service": "backend" | "cms",
     "method": "GET", "path": "/api/device-types/", "query": "a=1&b=2", "body": <JSON text> | null,
     "form": {field: value} | null, "files": {field: [filename, text]} | null, "write": false, "normalise": ["rows_by_id"]}

* ``path`` is the OLD public path (``/api/…``, ``/bom/api/…``, ``/studio-api/api/…``). The legacy URL is the service's base
  + the path (the CMS serves it without the ``/studio-api`` prefix, as nginx strips it today); the new URL is
  ``<new>/legacy`` + the path, exactly what nginx sends in ``shim`` mode (deploy/nginx/legacy/groups.conf).
* ``write: true`` requests change legacy data: they go to ``--legacy-backend-writes`` (a private legacy server on a
  private copy of the UAT dump), never to the shared UAT servers.
* Bodies are compared as JSON **text with key order** (integers and floats kept apart), after the normalisers named in
  the case — each one is an explicitly allowed volatile field (see ``NORMALISERS``). HTML bodies (legacy Django 404/500
  pages) are compared by status only and reported.
* ``approved.json`` lists approved differences (case id or id prefix → reason). Anything else fails the run.

    python tools/parity/harness.py --corpus tools/parity/corpus.jsonl --legacy-backend http://127.0.0.1:18012 \\
        --legacy-backend-writes http://127.0.0.1:18151 --legacy-cms http://127.0.0.1:18009 --new http://127.0.0.1:18150 \\
        --report docs/migration/parity-report.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from collections import Counter, OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
TIMEOUT = 60


# ── normalisers: the only fields allowed to differ (each is volatile by nature) ─────────────────────────────────────
def _rows(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        return body["data"]
    return None


def rows_by_id(body):
    """The legacy list endpoints had no ORDER BY (heap order): compare the rows ordered by ``id``."""
    rows = _rows(body)
    if rows is not None:
        rows.sort(key=lambda row: (row.get("id") is None, row.get("id") if isinstance(row, dict) else 0))
    return body


def _blank(keys):
    def blank(value):
        if isinstance(value, dict):
            for key in list(value):
                if key in keys:
                    value[key] = "<volatile>"
                else:
                    blank(value[key])
        elif isinstance(value, list):
            for item in value:
                blank(item)
        return value

    return blank


def ties_by_id(field: str):
    """Rows with EQUAL values of the sort column ``field`` in id order (the legacy ORDER BY had no tie-breaker: the
    heap decided). The order of the distinct values — the sort itself — is still compared."""

    def normalise(body):
        rows = _rows(body)
        if rows is None:
            return body
        out, run = [], []
        for row in rows:
            if run and row.get(field) != run[0].get(field):
                out += sorted(run, key=lambda item: item.get("id") or 0)
                run = []
            run.append(row)
        rows[:] = out + sorted(run, key=lambda item: item.get("id") or 0)
        return body

    return normalise


def normaliser(name: str):
    if name.startswith("ties_by_id:"):
        return ties_by_id(name.split(":", 1)[1])
    return NORMALISERS[name][0]


NORMALISERS = {
    "ties_by_id:<column>": (None, "legacy ORDER BY <column> without a tie-breaker (heap order for equal values): equal-valued rows compared in id order"),
    "rows_by_id": (rows_by_id, "legacy lists without ORDER BY (heap order): rows compared ordered by id"),
    "created_row": (_blank({"id", "created_at", "updated_at"}), "a row created by this request: its new id and timestamps"),
    "timestamps": (_blank({"created_at", "updated_at"}), "row timestamps written by the import / the request"),
    "otp_expiry": (_blank({"expires_at", "verification_token"}), "OTP expiry instant and token"),
}


# ── HTTP ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def _multipart(form: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (form or {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, (filename, text) in (files or {}).items():
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        parts.append(head.encode() + text.encode("latin-1") + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send(base: str, case: dict, index: int) -> dict:
    url = base + case["path"] + (("?" + case["query"]) if case.get("query") else "")
    data, headers = None, {"Accept": "application/json", "X-Forwarded-For": f"10.{(index >> 16) & 255}.{(index >> 8) & 255}.{index & 255}"}
    if case.get("files") or case.get("form"):
        data, headers["Content-Type"] = _multipart(case.get("form"), case.get("files"))
    elif case.get("body") is not None:
        data, headers["Content-Type"] = case["body"].encode(), "application/json"
    request = urllib.request.Request(url, data=data, method=case["method"], headers=headers)
    try:
        with OPENER.open(request, timeout=TIMEOUT) as response:
            status, raw, ctype, location = response.status, response.read(), response.headers.get("Content-Type", ""), response.headers.get("Location")
    except urllib.error.HTTPError as error:
        status, raw, ctype, location = error.code, error.read(), error.headers.get("Content-Type", ""), error.headers.get("Location")
    return {"status": status, "raw": raw, "ctype": ctype.split(";")[0].strip(), "location": location}


def parse(result: dict):
    if result["ctype"] != "application/json" or not result["raw"]:
        return None
    try:
        return json.loads(result["raw"], object_pairs_hook=OrderedDict)
    except ValueError:
        return None


def canonical(body) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def location_path(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    return parts.path + (("?" + parts.query) if parts.query else "")


def compare(case: dict, legacy: dict, new: dict) -> str | None:
    """``None`` when equal after the case's normalisers, else a one-line description of the first difference."""
    if legacy["status"] != new["status"]:
        return f"status {legacy['status']} ≠ {new['status']}"
    if legacy["status"] in (301, 302):
        old, got = location_path(legacy["location"]), location_path(new["location"])
        return None if old == got else f"Location {old} ≠ {got}"
    if legacy["ctype"] != "application/json":
        return None if legacy["status"] >= 400 else f"legacy {legacy['ctype']} body"
    old_body, new_body = parse(legacy), parse(new)
    if new_body is None:
        return f"content type {legacy['ctype']} ≠ {new['ctype']}"
    for name in case.get("normalise", []):
        old_body, new_body = normaliser(name)(old_body), normaliser(name)(new_body)
    old_text, new_text = canonical(old_body), canonical(new_body)
    if old_text == new_text:
        return None
    at = next((i for i, (a, b) in enumerate(zip(old_text, new_text)) if a != b), min(len(old_text), len(new_text)))
    return f"body differs at {at}: legacy …{old_text[max(0, at - 60):at + 80]}… new …{new_text[max(0, at - 60):at + 80]}…"


def _mask(body, path: list[str]) -> None:
    for key in path[:-1]:
        body = body.get(key) if isinstance(body, dict) else None
    if isinstance(body, dict) and path[-1] in body:
        body[path[-1]] = "<approved difference>"


def approval(case: dict, legacy: dict, new: dict, approved: list[dict]) -> dict | None:
    """The first approved difference that names this case AND whose constraints hold:

    * ``statuses: [legacy, new]`` — the only status pair approved;
    * ``drop_keys: [...]`` — these top-level keys are absent from the new body; everything else must be identical;
    * ``mask: ["data.resume", …]`` — these dotted paths differ deliberately; everything else must be identical;
    * with neither, the bodies are not compared (only used where the status itself differs).
    """
    for entry in approved:
        named = case["id"] == entry.get("id") or (entry.get("prefix") and case["id"].startswith(entry["prefix"])) or (entry.get("pattern") and re.search(entry["pattern"], case["id"]))
        if not named:
            continue
        if "statuses" in entry and [legacy["status"], new["status"]] != entry["statuses"]:
            continue
        if "drop_keys" in entry:
            old_body, new_body = parse(legacy), parse(new)
            if not isinstance(old_body, dict) or not isinstance(new_body, dict) or any(key in new_body for key in entry["drop_keys"]):
                continue
            for key in entry["drop_keys"]:
                old_body.pop(key, None)
            if compare(case, dict(legacy, raw=canonical(old_body).encode()), new) is not None:
                continue
        if "mask" in entry:
            old_body, new_body = parse(legacy), parse(new)
            if old_body is None or new_body is None:
                continue
            for dotted in entry["mask"]:
                _mask(old_body, dotted.split("."))
                _mask(new_body, dotted.split("."))
            if compare(case, dict(legacy, raw=canonical(old_body).encode()), dict(new, raw=canonical(new_body).encode())) is not None:
                continue
        return entry
    return None


def run(args) -> int:
    cases = [json.loads(line) for path in args.corpus for line in Path(path).read_text().splitlines() if line.strip()]
    approved = json.loads(Path(args.approved).read_text())
    bases = {"backend": args.legacy_backend, "cms": args.legacy_cms}
    if urlsplit(args.legacy_backend_writes).port in (18012, 18009):
        raise SystemExit("write-path requests must go to a PRIVATE legacy server (never the shared UAT servers)")
    results = []
    for index, case in enumerate(cases, start=1):
        if args.only and not case["id"].startswith(tuple(args.only)):
            continue
        base = args.legacy_backend_writes if case.get("write") else bases[case["service"]]
        legacy_case = dict(case, path=case["path"].removeprefix("/studio-api")) if case["service"] == "cms" else case
        legacy, new = send(base, legacy_case, index), send(args.new + "/legacy", case, index)
        difference = compare(case, legacy, new)
        entry = approval(case, legacy, new, approved) if difference else None
        results.append({"case": case, "legacy": legacy["status"], "new": new["status"], "difference": difference, "approved": entry})
    write_report(args, results, approved)
    failing = [result for result in results if result["difference"] and not result["approved"]]
    for result in failing:
        print(f"DIFF {result['case']['id']}: {result['difference']}"[:400])
    print(f"{len(results)} requests, {sum(1 for r in results if not r['difference'])} identical, {sum(1 for r in results if r['approved'])} approved differences, {len(failing)} unapproved")
    return 1 if failing else 0


def _relative(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(HERE.parents[1]))
    except ValueError:
        return path


def write_report(args, results: list[dict], approved: list[dict]) -> None:
    if not args.report:
        return
    groups = Counter(result["case"]["group"] for result in results)
    same = Counter(result["case"]["group"] for result in results if not result["difference"])
    ok_diff = Counter(result["case"]["group"] for result in results if result["approved"])
    methods = Counter(result["case"]["method"] for result in results)
    lines = [
        "# Legacy shim parity report",
        "",
        f"Generated by `tools/parity/harness.py` on {dt.date.today().isoformat()} from `{', '.join(_relative(path) for path in args.corpus)}` "
        f"({len(results)} requests: {', '.join(f'{count} {method}' for method, count in sorted(methods.items()))}).",
        "",
        f"* legacy main backend: `{args.legacy_backend}` (shared UAT server, read-only requests); write-path requests: "
        f"`{args.legacy_backend_writes}` (private legacy server on a private restore of `legacy_goldenapp.dump`)",
        f"* legacy CMS: `{args.legacy_cms}` (shared UAT server, read-only)",
        f"* new: `{args.new}/legacy/…` (the shim with `LEGACY_API_SHIM` on; database loaded from private restores of the same "
        "dumps with `import_cms` / `import_backend`, see docs/decisions/legacy-shim.md)",
        "",
        "## Summary per group",
        "",
        "| Group | Requests | Identical | Approved differences | Unapproved |",
        "|---|---|---|---|---|",
    ]
    for group in sorted(groups):
        unapproved = groups[group] - same[group] - ok_diff[group]
        lines.append(f"| {group} | {groups[group]} | {same[group]} | {ok_diff[group]} | {unapproved} |")
    total_same, total_ok = sum(same.values()), sum(ok_diff.values())
    lines.append(f"| **total** | **{len(results)}** | **{total_same}** | **{total_ok}** | **{len(results) - total_same - total_ok}** |")
    lines += ["", "## Normalisations applied (explicitly allowed volatile fields)", "", "| Name | Cases | What |", "|---|---|---|"]
    used = Counter(("ties_by_id:<column>" if name.startswith("ties_by_id:") else name) for result in results for name in result["case"].get("normalise", []))
    for name, (_, reason) in NORMALISERS.items():
        lines.append(f"| `{name}` | {used[name]} | {reason} |")
    lines += ["", "## Approved differences", "", "| Rule | Cases | Reason |", "|---|---|---|"]
    hits = Counter(id(result["approved"]) for result in results if result["approved"])
    for entry in approved:
        rule = entry.get("id") or (entry.get("prefix") and entry["prefix"] + "*") or f"re:{entry.get('pattern')}"
        if "statuses" in entry:
            rule += f" ({entry['statuses'][0]} → {entry['statuses'][1]})"
        lines.append(f"| `{rule}` | {hits[id(entry)]} | {entry['reason']} |")
    lines += ["", "## Differences", ""]
    shown = [result for result in results if result["difference"]]
    if not shown:
        lines.append("None.")
    else:
        lines += ["| Case | Legacy | New | Approved | Difference |", "|---|---|---|---|---|"]
        for result in shown:
            text = result["difference"].replace("|", "\\|").replace("\n", " ")[:220]
            lines.append(f"| `{result['case']['id']}` | {result['legacy']} | {result['new']} | {'yes' if result['approved'] else '**NO**'} | {text} |")
    Path(args.report).write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", nargs="+", default=[str(HERE / "corpus.jsonl")])
    parser.add_argument("--approved", default=str(HERE / "approved.json"))
    parser.add_argument("--legacy-backend", default="http://127.0.0.1:18012")
    parser.add_argument("--legacy-backend-writes", required=True, help="PRIVATE legacy main backend for write-path requests")
    parser.add_argument("--legacy-cms", default="http://127.0.0.1:18009")
    parser.add_argument("--new", required=True, help="new API base URL (the shim is under /legacy/)")
    parser.add_argument("--only", nargs="*", help="run only case ids with these prefixes")
    parser.add_argument("--report", help="write the markdown report here")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
