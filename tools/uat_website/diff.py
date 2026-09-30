"""Diff legacy vs platform captures in results/*.json. Usage: python diff.py [prefix] [-v]"""

import difflib
import json
import os
import re
import sys
from pathlib import Path

RES = Path(os.environ.get("UAT_DIR", ".")) / "results"
verbose = "-v" in sys.argv
args = [a for a in sys.argv[1:] if a != "-v"]
prefix = args[0] if args else ""

PORTS = re.compile(r"127\.0\.0\.1:18(31|32)[01]")
PHONE = re.compile(r"(\+91)?90[12]\d{7}")
CHUNK = re.compile(r"/_next/static/chunks/[^ )]+")


def scrub(s):
    return CHUNK.sub("CHUNK", PHONE.sub("PHONE", PORTS.sub("ORIGIN", s or "")))


def norm_text(t):
    t = scrub(t)
    return [line.strip() for line in t.splitlines() if line.strip()]


def jload(s):
    try:
        return json.loads(scrub(s))
    except Exception:
        return s


def api_key(a):
    req = a["request"] or ""
    if req.startswith("------"):  # multipart: boundary differs per request
        req = "multipart"
    return (a["method"], a["url"], scrub(req))


def json_diff(a, b, path="$", out=None):
    out = [] if out is None else out
    if type(a) is not type(b):
        out.append(f"{path}: {short(a)} != {short(b)}")
    elif isinstance(a, dict):
        for k in list(a) + [k for k in b if k not in a]:
            if k not in b:
                out.append(f"{path}.{k} only legacy")
            elif k not in a:
                out.append(f"{path}.{k} only platform")
            else:
                json_diff(a[k], b[k], f"{path}.{k}", out)
        if not out and list(a) != list(b):
            out.append(f"{path}: key order {list(a)} != {list(b)}")
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: len {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            json_diff(x, y, f"{path}[{i}]", out)
    elif a != b:
        out.append(f"{path}: {short(a)} != {short(b)}")
    return out


def short(v):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) < 120 else s[:117] + "..."


def keep(kind, x):
    """Drop environment noise: external hosts are unreachable here (egress policy) on both sides alike; the
    browser's generic "Failed to load resource" console line is covered by failed/httpErrors with the URL."""
    if kind == "console":
        x["text"] = scrub(x["text"].split("\n")[0])
        return not x["text"].startswith("Failed to load resource")
    if kind in ("failed", "httpErrors"):
        return x["url"].startswith("/")
    return True


def extras(e, path):
    out = {}
    if isinstance(e, dict):
        if isinstance(e.get("text"), str):
            out[path] = e["text"]
        else:
            for k, v in e.items():
                if isinstance(v, str) and k.startswith("tile"):
                    out[f"{path}.{k}"] = v
                else:
                    out.update(extras(v, f"{path}.{k}"))
    return out


def compare(rec):
    L, P = rec.get("legacy", {}), rec.get("platform", {})
    issues = []
    for k in ("status", "error", "title", "canonical"):
        if L.get(k) != P.get(k):
            issues.append(f"{k}: {L.get(k)!r} != {P.get(k)!r}")
    lt, pt = norm_text(L.get("text")), norm_text(P.get("text"))
    if lt != pt:
        d = [x for x in difflib.unified_diff(lt, pt, "legacy", "platform", n=0, lineterm="") if not x.startswith("@@")]
        issues.append("text differs:\n      " + "\n      ".join(d[2:40]))
    if L.get("ld") != P.get("ld"):
        issues.append("json-ld: " + "; ".join(json_diff(L.get("ld"), P.get("ld"))[:10]))
    if L.get("meta") != P.get("meta"):
        issues.append("meta: " + "; ".join(json_diff(L.get("meta"), P.get("meta"))[:10]))
    for k in ("console", "pageErrors", "failed", "httpErrors"):
        a = sorted(json.dumps(x, sort_keys=True) for x in L.get(k, []) if keep(k, x))
        b = sorted(json.dumps(x, sort_keys=True) for x in P.get(k, []) if keep(k, x))
        if set(a) != set(b):
            only_l = sorted(set(a) - set(b))
            only_p = sorted(set(b) - set(a))
            issues.append(f"{k}: only legacy {only_l[:5]} | only platform {only_p[:5]}")
    for k, v in extras(L.get("extra"), "extra").items():
        w = extras(P.get("extra"), "extra").get(k)
        if w is None or norm_text(v) != norm_text(w):
            d = [x for x in difflib.unified_diff(norm_text(v), norm_text(w or ""), "legacy", "platform", n=0, lineterm="") if not x.startswith("@@")]
            issues.append(f"{k} text differs:\n      " + "\n      ".join(d[2:30]))
    la = {}
    for a in L.get("api", []):
        la.setdefault(api_key(a), []).append(a)
    pa = {}
    for a in P.get("api", []):
        pa.setdefault(api_key(a), []).append(a)
    for key in sorted(set(la) | set(pa)):
        if key not in pa:
            issues.append(f"api only legacy: {key[0]} {key[1]}")
            continue
        if key not in la:
            issues.append(f"api only platform: {key[0]} {key[1]}")
            continue
        x, y = la[key][0], pa[key][0]
        if x["status"] != y["status"]:
            issues.append(f"api {key[0]} {key[1]}: status {x['status']} != {y['status']}")
        dj = json_diff(jload(x["body"]), jload(y["body"]))
        if dj:
            issues.append(f"api {key[0]} {key[1]}: " + "; ".join(dj[:12]))
    return issues, len(la), len(pa)


total = 0
for f in sorted(RES.glob(f"{prefix}*.json")):
    rec = json.loads(f.read_text())
    issues, nl, np_ = compare(rec)
    total += bool(issues)
    tag = "DIFF" if issues else "same"
    print(f"{tag:4} {f.stem}  (api calls {nl}/{np_})")
    if issues and (verbose or len(issues) < 6):
        for i in issues:
            print("   -", i[:3000])
print("differing:", total)
