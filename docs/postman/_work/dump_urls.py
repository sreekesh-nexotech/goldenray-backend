"""Source 1 of the endpoint inventory: the live Django URL resolver.

Runs inside the API container (Django is not installed on the host):

    docker exec flarize-dev-api-1 python docs/postman/_work/dump_urls.py

Walks ``get_resolver().url_patterns`` recursively (every include, namespace and router), and writes
``docs/postman/_work/resolver.json``: one row per (method, canonical path). Methods come from the framework itself:

* DRF ViewSets: ``callback.actions`` (the router's real method -> action map, ``@action`` routes included);
* DRF APIViews / Django CBVs: the handler methods defined on the class and allowed by ``http_method_names``;
* function views: a module-level ``ALLOWED_METHODS`` / ``SAFE_METHODS`` tuple the view checks, a
  ``require_http_methods`` closure, or (``django.views.static.serve``) GET.

OPTIONS and HEAD are recorded as excluded (automatic on every DRF view / mirror GET), not as rows.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent / "resolver.json"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flarize.settings.dev")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.urls import URLPattern, URLResolver, get_resolver  # noqa: E402
from rest_framework.views import APIView  # noqa: E402

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
EXCLUDED_METHODS = ("head", "options", "trace")
VERSION = settings.API_VERSIONS[-1] if getattr(settings, "API_VERSIONS", None) else "v1"


def pattern_regex(pattern) -> str:
    inner = pattern.pattern
    regex = getattr(inner, "regex", None)
    text = regex.pattern if regex is not None else str(inner)
    if text.startswith("^"):
        text = text[1:]
    return text


def converters(pattern) -> dict:
    return {name: type(conv).__name__.replace("Converter", "").lower() for name, conv in getattr(pattern.pattern, "converters", {}).items()}


def walk(patterns, prefix="", namespaces=(), convs=None):
    convs = convs or {}
    for pattern in patterns:
        text = pattern_regex(pattern)
        merged = {**convs, **converters(pattern)}
        if isinstance(pattern, URLResolver):
            if text.endswith("$"):
                text = text[:-1]
            ns = namespaces + ((pattern.namespace,) if pattern.namespace else ())
            yield from walk(pattern.url_patterns, prefix + text, ns, merged)
        elif isinstance(pattern, URLPattern):
            yield prefix + text, pattern, namespaces, merged


def split_groups(regex: str):
    """Tokenise a regex into literal text and top-level groups ``(kind, name, body)`` (balanced parentheses)."""
    tokens, i, literal = [], 0, ""
    while i < len(regex):
        ch = regex[i]
        if ch == "\\" and i + 1 < len(regex):
            literal += regex[i : i + 2]
            i += 2
            continue
        if ch == "(":
            depth, j = 1, i + 1
            while j < len(regex) and depth:
                if regex[j] == "\\":
                    j += 2
                    continue
                depth += {"(": 1, ")": -1}.get(regex[j], 0)
                j += 1
            group = regex[i + 1 : j - 1]
            optional = j < len(regex) and regex[j] == "?"
            if literal:
                tokens.append(("lit", literal))
                literal = ""
            m = re.match(r"\?P<(\w+)>(.*)$", group, re.S)
            if m:
                tokens.append(("named", m.group(1), m.group(2)))
            else:
                tokens.append(("anon", group.removeprefix("?:"), optional))
            i = j + (1 if optional else 0)
            continue
        literal += ch
        i += 1
    if literal:
        tokens.append(("lit", literal))
    return tokens


def expand(regex: str) -> list[tuple[str, dict]]:
    """Canonical path for a full URL regex: ``{name}`` for named groups (a plain alternation of words is recorded as the
    param's choices), the version group rendered literally, optional anonymous groups dropped."""
    variants = [("", {})]
    for tok in split_groups(regex):
        if tok[0] == "lit":
            text = tok[1]
            text = re.sub(r"\\Z$|\$$", "", text)
            text = text.replace("/?", "/").replace("\\.", ".").replace("\\-", "-")
            variants = [(p + text, a) for p, a in variants]
        elif tok[0] == "named":
            name, body = tok[1], tok[2]
            if name == "version":
                variants = [(p + VERSION, a) for p, a in variants]
            elif re.fullmatch(r"[A-Za-z0-9_\-]+(\|[A-Za-z0-9_\-]+)+", body):
                variants = [(p + "{" + name + "}", {**a, name: body.split("|")}) for p, a in variants]
            else:
                variants = [(p + "{" + name + "}", {**a, name: body}) for p, a in variants]
        else:
            body, optional = tok[1], tok[2]
            if not optional:
                variants = [(p + body, a) for p, a in variants]
    return [("/" + p.lstrip("/"), a) for p, a in variants]


def function_view_methods(func) -> tuple[list[str], str]:
    target = inspect.unwrap(func)
    module = sys.modules.get(target.__module__)
    for attr in ("ALLOWED_METHODS", "SAFE_METHODS"):
        value = getattr(module, attr, None)
        if value and attr in inspect.getsource(target):
            return [m.lower() for m in value if m.lower() in HTTP_METHODS], f"{target.__module__}.{attr}"
    closure_func = func
    while closure_func is not None:
        for cell in closure_func.__closure__ or ():
            contents = cell.cell_contents
            if isinstance(contents, (list, tuple)) and contents and all(isinstance(x, str) and x.isupper() for x in contents):
                return [m.lower() for m in contents if m.lower() in HTTP_METHODS], "require_http_methods"
        closure_func = getattr(closure_func, "__wrapped__", None)
    if target.__module__ == "django.views.static" and target.__name__ == "serve":
        return ["get"], "django.views.static.serve (GET)"
    return ["get"], "ASSUMED-GET"


def refusal(cls, kind: str, method: str, action) -> str | None:
    """Why a routed method can never succeed, or ``None`` — in DRF's own order: ``APIView.dispatch`` runs ``initial()``
    (authentication, then permissions) BEFORE the ``http_method_names`` check. So on a staff view an action/method that
    ``action_permissions`` does not map is 403 for every caller (core.permissions.required_permission: default deny);
    only a mapped one can reach DRF's 405 (``http_method_names``) or PatchOnlyUpdateMixin's 405."""
    if cls is None or kind not in ("viewset", "apiview"):
        return None
    perms = [p.__name__ if isinstance(p, type) else type(p).__name__ for p in getattr(cls, "permission_classes", [])]
    if "HasModulePermission" in perms:
        mapping = getattr(cls, "action_permissions", None) or {}
        key = action if kind == "viewset" else method.upper()
        if key not in mapping:
            return f"403 unmapped {'action' if kind == 'viewset' else 'method'} '{key}' (default deny, checked before the method)"
    if method not in [m.lower() for m in getattr(cls, "http_method_names", HTTP_METHODS)]:
        return "405 method not in http_method_names (403 for callers without the mapped permission)"
    if action == "update" and any(c.__name__ == "PatchOnlyUpdateMixin" for c in cls.__mro__):
        return "405 PatchOnlyUpdateMixin: PUT refused, use PATCH (403 for callers without the mapped permission)"
    return None


def required_flag(callback, cls) -> str | None:
    """The feature flag a view answers 404 behind: ``FlagRequiredMixin.required_flag`` (DRF), the ``require_flag``
    wrapper attribute (plain views) or ``legacy.views.base.LegacyView`` (``LEGACY_API_SHIM``, checked in dispatch)."""
    if cls is not None:
        if getattr(cls, "required_flag", None):
            return cls.required_flag
        if any(c.__name__ == "LegacyView" for c in cls.__mro__):
            return sys.modules[cls.__mro__[[c.__name__ for c in cls.__mro__].index("LegacyView")].__module__].FLAG
        return None
    func = callback
    while func is not None:
        if getattr(func, "required_flag", None):
            return func.required_flag
        func = getattr(func, "__wrapped__", None)
    return None


def source_location(obj) -> str:
    try:
        obj = inspect.unwrap(obj)
        file = Path(inspect.getsourcefile(obj)).resolve()
        line = inspect.getsourcelines(obj)[1]
        try:
            return f"{file.relative_to(ROOT)}:{line}"
        except ValueError:
            return f"{file}:{line}"
    except (TypeError, OSError):
        module = sys.modules.get(getattr(obj, "__module__", ""))
        file = getattr(module, "__file__", None)
        return f"{Path(file).resolve().relative_to(ROOT)} (built dynamically)" if file and str(file).startswith(str(ROOT)) else ""


def surface_for(path: str, namespaces: tuple) -> str:
    if namespaces:
        return namespaces[0]
    if path == "/healthz" or path.startswith("/api/schema/") or path.startswith("/api/docs/"):
        return "ops"
    if path.startswith(f"/{settings.PUBLIC_MEDIA_URL.strip('/')}/"):
        return "dev-media"
    return "root"


def main():
    rows, excluded = [], []
    for pattern_index, (regex, pattern, namespaces, convs) in enumerate(walk(get_resolver().url_patterns)):
        callback = pattern.callback
        cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
        actions = getattr(callback, "actions", None)
        if actions:
            method_map = {m: a for m, a in actions.items()}
            kind, method_source = "viewset", "callback.actions"
        elif cls is not None:
            allowed = [m.lower() for m in getattr(cls, "http_method_names", HTTP_METHODS)]
            method_map = {m: m for m in allowed if hasattr(cls, m)}
            kind = "apiview" if issubclass(cls, APIView) else "django-cbv"
            method_source = "class handlers"
        else:
            methods, method_source = function_view_methods(callback)
            method_map = {m: None for m in methods}
            kind = "function"
        target = cls or callback
        view_path = f"{target.__module__}.{target.__qualname__}"
        app = target.__module__.split(".")[0]
        class_location = source_location(cls or callback)
        for path, fixed in expand(regex):
            refusals = {m: refusal(cls, kind, m, a) for m, a in method_map.items()}
            for method, action in method_map.items():
                handler = getattr(cls, action or method, None) if cls is not None else None
                location = (source_location(handler) if callable(handler) else "") or class_location
                if method in EXCLUDED_METHODS:
                    excluded.append({"method": method.upper(), "path": path, "reason": "automatic HEAD/OPTIONS"})
                    continue
                if method not in HTTP_METHODS:
                    continue
                params = {name: convs.get(name, "regex") for name in re.findall(r"{(\w+)}", path)}
                rows.append(
                    {
                        "method": method.upper(),
                        "path": path,
                        "regex": regex,
                        "pattern_index": pattern_index,  # urlconf order: Django resolves to the FIRST matching pattern
                        "url_name": pattern.name,
                        "namespace": ":".join(namespaces),
                        "surface": surface_for(path, namespaces),
                        "view": view_path,
                        "view_kind": kind,
                        "action": action,
                        "app": app,
                        "source_file": location,
                        "method_source": method_source,
                        "path_params": params,
                        "refused": refusals.get(method),
                        "flag": required_flag(callback, cls),
                        "fixed_params": fixed,
                    }
                )
    rows.sort(key=lambda r: (r["path"], r["method"]))
    seen, dupes = set(), []
    for r in rows:
        key = (r["method"], r["path"])
        if key in seen:
            dupes.append(key)
        seen.add(key)
    OUT.write_text(json.dumps({"rows": rows, "excluded_methods": excluded, "duplicates": dupes, "debug": settings.DEBUG}, indent=1) + "\n")
    print(f"resolver rows: {len(rows)}  excluded HEAD/OPTIONS: {len(excluded)}  duplicate (method,path): {len(dupes)}")
    print(f"refused (routed but never succeeds): {sum(1 for r in rows if r['refused'])}")
    assumed = [r for r in rows if r["method_source"] == "ASSUMED-GET"]
    if assumed:
        print("WARNING assumed GET for:", [r["view"] for r in assumed])


if __name__ == "__main__":
    main()
