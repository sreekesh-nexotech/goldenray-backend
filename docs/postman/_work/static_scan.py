"""Source 3 of the endpoint inventory: a static scan of the source tree (no Django import; host python3).

    python3 docs/postman/_work/static_scan.py

1. Greps every project ``*.py`` (tests, docs/ and the standalone essl-agent package excluded) for URL-defining constructs:
   ``path(``, ``re_path(``, ``url(``, ``router.register``, ``@action``, ``@api_view``, ``as_view``, ``include(``,
   ``urlpatterns +=``, ``.urls``, ``static(``, ``require_http_methods``.
2. Parses every ``urls.py`` with ``ast`` and every ``views`` module for ``@action`` / ``.mapping.<method>`` routes, then
   asserts each statically declared route is present in ``resolver.json`` (source 1):
   * every ``router.register(prefix, ViewSet)``  -> a resolver row for that ViewSet under ``/<prefix>/``;
   * every ``X.as_view()`` / function view in ``path()``/``re_path()`` -> a resolver row for that view;
   * every literal ``path()`` route -> a resolver path ending in it;
   * every ``@action`` (and ``@<action>.mapping.<method>``) -> a resolver row with that action name and method(s);
   * every ``*_urlpatterns`` list name is one flarize/urls.py mounts; every app with a urls.py is in LOCAL_APPS.
   Dynamic constructs (views built with ``type()``, dict-driven ``path()`` loops) are listed for the report.
Writes ``static_scan.json``; exits 1 if any statically declared route is absent from the resolver.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORK = Path(__file__).resolve().parent
SKIP_DIRS = {"docs", "essl-agent", "var", "node_modules", ".git", "migrations", "deploy", "tools", "scripts"}
GREP = {
    "path(": r"(?<![\w.])path\(",
    "re_path(": r"\bre_path\(",
    "url(": r"(?<![\w.])url\(",
    "router.register": r"\.register\(",
    "@action": r"@action\(",
    "@api_view": r"@api_view",
    "as_view": r"\.as_view\(",
    "include(": r"\binclude\(",
    "urlpatterns +=": r"urlpatterns\s*\+=",
    ".urls": r"\.urls\b",
    "static(": r"(?<![\w.])static\(",
    "require_http_methods": r"require_(http_methods|GET|POST|safe)",
}
STANDARD_LISTS = {"staff_urlpatterns", "public_urlpatterns", "agent_urlpatterns", "customer_urlpatterns", "iclock_urlpatterns", "legacy_urlpatterns", "urlpatterns"}
HTTP = {"get", "post", "put", "patch", "delete"}


def project_files():
    for file in sorted(ROOT.rglob("*.py")):
        rel = file.relative_to(ROOT)
        if rel.parts[0] in SKIP_DIRS or "tests" in rel.parts or any(p.startswith(".") for p in rel.parts):
            continue
        yield rel, file.read_text()


def canon_route(route: str) -> str:
    return re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", route)


def call_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def scan_urls(rel: Path, tree: ast.Module):
    registers, views, routes, dynamic, lists = [], [], [], [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id.endswith("urlpatterns"):
                    lists.append(t.id)
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node.func)
        if name == "register" and len(node.args) >= 2:
            prefix = node.args[0].value if isinstance(node.args[0], ast.Constant) else None
            prefix_regex = None
            if isinstance(node.args[0], ast.JoinedStr):
                prefix_regex = "".join(re.escape(v.value) if isinstance(v, ast.Constant) else "[^/]+" for v in node.args[0].values)
                dynamic.append({"file": str(rel), "line": node.lineno, "what": f"router.register() with computed prefix {prefix_regex!r}"})
            registers.append({"prefix": prefix, "prefix_regex": prefix_regex, "viewset": call_name(node.args[1]), "line": node.lineno})
        elif name in ("path", "re_path") and len(node.args) >= 2:
            route = node.args[0]
            target = node.args[1]
            literal = route.value if isinstance(route, ast.Constant) else None
            if isinstance(target, ast.Call) and call_name(target.func) == "as_view":
                owner = target.func.value
                view = call_name(owner) if isinstance(owner, (ast.Name, ast.Attribute)) else ""
                if isinstance(owner, ast.Name) and owner.id[:1].islower():
                    dynamic.append({"file": str(rel), "line": node.lineno, "what": f"{name}() with view variable '{owner.id}' (dict-driven)"})
                    view = ""
            elif isinstance(target, ast.Call) and call_name(target.func) == "include":
                view = ""
            else:
                view = call_name(target)
            if view:
                views.append({"view": view, "line": node.lineno, "kind": name})
            if literal is None:
                dynamic.append({"file": str(rel), "line": node.lineno, "what": f"{name}() with computed route"})
            elif name == "path" and not (isinstance(target, ast.Call) and call_name(target.func) == "include"):
                routes.append({"route": canon_route(literal), "line": node.lineno, "view": view})
    return registers, views, routes, dynamic, lists


def scan_actions(rel: Path, tree: ast.Module):
    actions, dynamic = [], []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in fn.decorator_list:
                if isinstance(dec, ast.Call) and call_name(dec.func) == "action":
                    kw = {k.arg: k.value for k in dec.keywords}
                    methods = ["get"]
                    if "methods" in kw and isinstance(kw["methods"], (ast.List, ast.Tuple)):
                        methods = [e.value.lower() for e in kw["methods"].elts if isinstance(e, ast.Constant)]
                    url_path = kw["url_path"].value if isinstance(kw.get("url_path"), ast.Constant) else fn.name
                    detail = kw["detail"].value if isinstance(kw.get("detail"), ast.Constant) else None
                    actions.append({"file": str(rel), "line": fn.lineno, "class": cls.name, "action": fn.name, "methods": methods, "url_path": url_path, "detail": detail})
                elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and isinstance(dec.func.value, ast.Attribute) and dec.func.value.attr == "mapping":
                    actions.append(
                        {
                            "file": str(rel),
                            "line": fn.lineno,
                            "class": cls.name,
                            "action": fn.name,
                            "methods": [dec.func.attr],
                            "url_path": None,
                            "detail": None,
                            "mapping_of": call_name(dec.func.value.value),
                        }
                    )
        for node in ast.walk(cls):
            if isinstance(node, ast.Call) and call_name(node.func) == "type" and len(node.args) == 3:
                dynamic.append({"file": str(rel), "line": node.lineno, "what": "view class built with type()"})
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.DictComp):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Call) and call_name(sub.func) == "type":
                    dynamic.append({"file": str(rel), "line": node.lineno, "what": f"{node.targets[0].id}: view classes built with type() in a dict comprehension"})
                    break
    return actions, dynamic


def main():
    resolver = json.loads((WORK / "resolver.json").read_text())["rows"]
    by_class = {}
    for r in resolver:
        by_class.setdefault(r["view"].rsplit(".", 1)[-1], []).append(r)
    resolver_paths = {r["path"] for r in resolver}

    grep_hits, problems, dynamic, actions, url_modules = {}, [], [], [], {}
    for rel, text in project_files():
        counts = {k: len(re.findall(rx, text)) for k, rx in GREP.items()}
        counts = {k: v for k, v in counts.items() if v}
        if counts:
            grep_hits[str(rel)] = counts
        tree = ast.parse(text)
        if rel.name == "urls.py":
            registers, views, routes, dyn, lists = scan_urls(rel, tree)
            url_modules[str(rel)] = {"registers": registers, "views": views, "routes": routes, "lists": sorted(set(lists))}
            dynamic += dyn
            for reg in registers:
                rows = [r for r in by_class.get(reg["viewset"], []) if reg["prefix"] is None or f"/{reg['prefix']}/" in r["path"]]
                if not rows and reg["prefix"]:  # alias / factory-built ViewSet: the route itself must exist
                    rows = [r for r in resolver if re.search(rf"/v\d+/{re.escape(reg['prefix'])}/", r["path"])]
                if not rows and reg["prefix_regex"]:
                    rows = [r for r in resolver if re.search(rf"/v\d+/{reg['prefix_regex']}/", r["path"])]
                if not rows:
                    problems.append(f"{rel}:{reg['line']} router.register({reg['prefix']!r}, {reg['viewset']}) not in resolver")
            for v in views:
                if v["view"] not in by_class:
                    problems.append(f"{rel}:{v['line']} view {v['view']} not in resolver")
            for route in routes:
                if not any(p.endswith("/" + route["route"]) or p == "/" + route["route"] for p in resolver_paths):
                    problems.append(f"{rel}:{route['line']} path {route['route']!r} not in resolver")
            for name in lists:
                if name not in STANDARD_LISTS:
                    problems.append(f"{rel}: pattern list {name} is not mounted by flarize/urls.py")
        if "views" in rel.parts or rel.stem.startswith("views"):
            acts, dyn = scan_actions(rel, tree)
            actions += acts
            dynamic += dyn

    by_action = {}
    for r in resolver:
        by_action.setdefault(r["action"], []).append(r)
    for act in actions:
        rows = [r for r in by_action.get(act["action"], []) if r["view"].endswith("." + act["class"]) or act["class"].startswith("Base") or act["class"].endswith("Mixin")]
        if not rows:
            problems.append(f"{act['file']}:{act['line']} @action {act['class']}.{act['action']} not in resolver")
            continue
        got = {r["method"].lower() for r in rows}
        for m in act["methods"]:
            if m in HTTP and m not in got:
                problems.append(f"{act['file']}:{act['line']} @action {act['class']}.{act['action']} method {m.upper()} not in resolver")

    settings_text = (ROOT / "flarize/settings/base.py").read_text()
    for module in url_modules:
        app = module.split("/")[0]
        if app != "flarize" and f'"{app}"' not in settings_text:
            problems.append(f"{module}: app '{app}' not in LOCAL_APPS — its urls are never mounted")

    totals = Counter()
    for counts in grep_hits.values():
        totals.update(counts)
    out = {
        "grep_totals": dict(totals),
        "grep_by_file": grep_hits,
        "url_modules": url_modules,
        "actions": actions,
        "dynamic_constructs": dynamic,
        "problems": problems,
    }
    (WORK / "static_scan.json").write_text(json.dumps(out, indent=1) + "\n")
    print(f"urls modules: {len(url_modules)}  router.register: {sum(len(m['registers']) for m in url_modules.values())}  " f"@action/mapping: {len(actions)}  dynamic constructs: {len(dynamic)}")
    print("grep totals:", dict(totals))
    for d in dynamic:
        print("DYNAMIC", d["file"], d["line"], d["what"])
    for p in problems:
        print("PROBLEM", p)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
