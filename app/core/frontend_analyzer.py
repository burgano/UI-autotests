"""
Analyzes a local frontend project and extracts a compact route/component summary.
Supports: Next.js (pages & app router), React Router, Vue Router, Angular.
Output is a small JSON-serializable dict - never raw source files.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RouteInfo:
    path: str
    component_file: str
    form_fields: list[dict] = field(default_factory=list)  # {name, type, required, maxlength}
    buttons: list[str] = field(default_factory=list)
    api_calls: list[str] = field(default_factory=list)
    auth_required: bool = False
    framework: str = ""


def analyze(frontend_path: str) -> dict:
    """Return a compact summary dict suitable for injection into prompts."""
    if not os.path.isdir(frontend_path):
        return {"error": f"Path does not exist: {frontend_path}", "routes": []}

    framework = _detect_framework(frontend_path)
    routes = _extract_routes(frontend_path, framework)
    _enrich_routes(routes, frontend_path)

    return {
        "framework": framework,
        "routes": [_route_to_dict(r) for r in routes],
        "total_routes": len(routes),
    }


def _detect_framework(root: str) -> str:
    pkg = os.path.join(root, "package.json")
    if os.path.isfile(pkg):
        try:
            data = json.load(open(pkg))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if "next" in deps:
                return "nextjs"
            if "nuxt" in deps:
                return "nuxt"
            if "@angular/core" in deps:
                return "angular"
            if "vue" in deps:
                return "vue"
            if "react" in deps:
                return "react"
        except Exception:
            pass
    # Fallback: directory heuristics
    if os.path.isdir(os.path.join(root, "pages")) or os.path.isdir(os.path.join(root, "app")):
        return "nextjs"
    return "unknown"


def _extract_routes(root: str, framework: str) -> list[RouteInfo]:
    if framework == "nextjs":
        return _next_routes(root)
    if framework == "nuxt":
        return _nuxt_routes(root)
    if framework == "angular":
        return _angular_routes(root)
    if framework == "vue":
        return _vue_routes(root)
    if framework == "react":
        return _react_router_routes(root)
    return []


def _next_routes(root: str) -> list[RouteInfo]:
    routes = []
    # App router
    app_dir = os.path.join(root, "app")
    pages_dir = os.path.join(root, "pages")

    if os.path.isdir(app_dir):
        for dirpath, _, files in os.walk(app_dir):
            for f in files:
                if f in ("page.tsx", "page.jsx", "page.js", "page.ts"):
                    rel = os.path.relpath(dirpath, app_dir)
                    url_path = "/" if rel == "." else "/" + rel.replace(os.sep, "/")
                    url_path = re.sub(r"\[([^\]]+)\]", r":\1", url_path)  # [id] -> :id
                    routes.append(RouteInfo(
                        path=url_path,
                        component_file=os.path.join(dirpath, f),
                        framework="nextjs",
                    ))
    elif os.path.isdir(pages_dir):
        for dirpath, _, files in os.walk(pages_dir):
            for f in files:
                if f.endswith((".tsx", ".jsx", ".js", ".ts")) and not f.startswith("_"):
                    rel = os.path.relpath(os.path.join(dirpath, f), pages_dir)
                    url_path = "/" + re.sub(r"\.[^.]+$", "", rel).replace(os.sep, "/")
                    if url_path.endswith("/index"):
                        url_path = url_path[:-6] or "/"
                    url_path = re.sub(r"\[([^\]]+)\]", r":\1", url_path)
                    routes.append(RouteInfo(
                        path=url_path,
                        component_file=os.path.join(dirpath, f),
                        framework="nextjs",
                    ))
    return routes


def _nuxt_routes(root: str) -> list[RouteInfo]:
    routes = []
    pages_dir = os.path.join(root, "pages")
    if not os.path.isdir(pages_dir):
        return routes
    for dirpath, _, files in os.walk(pages_dir):
        for f in files:
            if f.endswith((".vue", ".js", ".ts")):
                rel = os.path.relpath(os.path.join(dirpath, f), pages_dir)
                url_path = "/" + re.sub(r"\.[^.]+$", "", rel).replace(os.sep, "/")
                if url_path.endswith("/index"):
                    url_path = url_path[:-6] or "/"
                routes.append(RouteInfo(path=url_path, component_file=os.path.join(dirpath, f), framework="nuxt"))
    return routes


def _vue_routes(root: str) -> list[RouteInfo]:
    routes = []
    for candidate in ["src/router/index.js", "src/router/index.ts", "router/index.js", "router/index.ts"]:
        router_file = os.path.join(root, candidate)
        if os.path.isfile(router_file):
            content = open(router_file, encoding="utf-8", errors="ignore").read()
            for m in re.finditer(r"path\s*:\s*['\"]([^'\"]+)['\"]", content):
                routes.append(RouteInfo(path=m.group(1), component_file=router_file, framework="vue"))
            break
    return routes


def _angular_routes(root: str) -> list[RouteInfo]:
    routes = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            if "routing" in f.lower() and f.endswith((".ts", ".js")):
                content = open(os.path.join(dirpath, f), encoding="utf-8", errors="ignore").read()
                for m in re.finditer(r"path\s*:\s*['\"]([^'\"]*)['\"]", content):
                    path = "/" + m.group(1) if not m.group(1).startswith("/") else m.group(1)
                    if path not in ("/**", "/*"):
                        routes.append(RouteInfo(path=path, component_file=os.path.join(dirpath, f), framework="angular"))
    return routes


def _react_router_routes(root: str) -> list[RouteInfo]:
    routes = []
    path_pattern = re.compile(r'(?:path|to)\s*[:=]\s*["\']([/][^"\']*)["\']')
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "dist", "build")]
        for f in files:
            if f.endswith((".tsx", ".jsx", ".js", ".ts")):
                fpath = os.path.join(dirpath, f)
                content = open(fpath, encoding="utf-8", errors="ignore").read()
                if "Route" in content or "createBrowserRouter" in content:
                    for m in path_pattern.finditer(content):
                        p = m.group(1)
                        if p and p not in [r.path for r in routes]:
                            routes.append(RouteInfo(path=p, component_file=fpath, framework="react"))
    return routes


def _enrich_routes(routes: list[RouteInfo], root: str):
    """Add form fields, buttons, API calls, auth flags from component source."""
    input_re    = re.compile(r'<input[^>]*>', re.IGNORECASE)
    type_re     = re.compile(r'type=["\']([^"\']+)["\']')
    name_re     = re.compile(r'(?:name|id|placeholder|aria-label)=["\']([^"\']+)["\']')
    maxlen_re   = re.compile(r'maxlength=["\'](\d+)["\']', re.IGNORECASE)
    required_re = re.compile(r'\brequired\b', re.IGNORECASE)
    button_re   = re.compile(r'<button[^>]*>(.*?)</button>', re.IGNORECASE | re.DOTALL)
    api_re      = re.compile(r'(?:fetch|axios\.(?:get|post|put|delete|patch))\s*\(["\']([^"\']+)["\']')
    auth_re     = re.compile(r'(authGuard|PrivateRoute|requireAuth|isAuthenticated|middleware.*auth)', re.IGNORECASE)

    for route in routes:
        if not os.path.isfile(route.component_file):
            continue
        try:
            content = open(route.component_file, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue

        for inp in input_re.findall(content):
            field_info = {
                "type":     (type_re.search(inp) or type("", (object,), {"group": lambda s, n: "text"})()).group(1) if type_re.search(inp) else "text",
                "name":     name_re.search(inp).group(1) if name_re.search(inp) else "",
                "required": bool(required_re.search(inp)),
                "maxlength": int(maxlen_re.search(inp).group(1)) if maxlen_re.search(inp) else None,
            }
            route.form_fields.append(field_info)

        route.buttons = [
            re.sub(r"<[^>]+>", "", b).strip()[:50]
            for b in button_re.findall(content)
            if re.sub(r"<[^>]+>", "", b).strip()
        ][:10]

        route.api_calls = list({m.group(1) for m in api_re.finditer(content)})[:10]
        route.auth_required = bool(auth_re.search(content))


def _route_to_dict(r: RouteInfo) -> dict:
    return {
        "path":           r.path,
        "component_file": os.path.basename(r.component_file),
        "form_fields":    r.form_fields,
        "buttons":        r.buttons,
        "api_calls":      r.api_calls,
        "auth_required":  r.auth_required,
    }
