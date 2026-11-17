"""What a project is, from its file list (and a few small files): one decision used by
GitHub deployments, ZIP uploads and the repository review.

``choose()`` picks the framework preset to build with (or None when there is nothing runnable),
``describe_non_web()`` says honestly what a project without a web application contains, and
``classify()`` sorts a project into the review categories. Nothing here builds or runs code.
"""
from __future__ import annotations

import json
import re

from . import multiservice
from .gitinfo import FRAMEWORKS, _requirement_names, detect_framework

# Python packages that serve a web page or an API.
PY_WEB = {"flask", "fastapi", "django", "streamlit", "gradio", "dash", "uvicorn", "gunicorn", "aiohttp",
          "bottle", "tornado", "panel", "voila", "starlette", "litestar", "sanic", "quart", "nicegui",
          "shiny", "chainlit", "falcon", "pyramid", "cherrypy", "waitress", "hypercorn"}
SITE_DIRS = ("dist", "build", "public", "out", "www", "site", "_site", "docs")
MARKERS = ("package.json", "requirements.txt", "manage.py", "Dockerfile", "pyproject.toml", *multiservice.COMPOSE_NAMES)


def top_level(files) -> set:
    return {f for f in files if "/" not in f}


def compose_services(text: str | None) -> int:
    """Services that start by default (0 when the file is missing or unreadable)."""
    if not text:
        return 0
    try:
        doc = multiservice.parse(text)
    except multiservice.ComposePlanError:
        return 0
    return sum(1 for s in doc["services"].values() if isinstance(s, dict) and not s.get("profiles"))


def _has_site(files) -> bool:
    top = top_level(files)
    return "index.html" in top or any(f"{d}/index.html" in files for d in SITE_DIRS) \
        or any(f.endswith((".html", ".htm")) for f in top)


def _previewable(files) -> bool:
    return any(f.endswith(".ipynb") for f in files) or any(
        f.lower() in ("readme.md", "readme.markdown", "readme.txt", "readme") for f in top_level(files))


def choose(files: set, read) -> str | None:
    """The preset to build with, or None. ``read(path)`` returns a small file's text or None."""
    top = top_level(files)
    compose = multiservice.find_compose(top)
    if compose and compose_services(read(compose)) >= 2:
        return "compose"
    pkg = None
    if "package.json" in top:
        try:
            pkg = json.loads(read("package.json") or "")
        except ValueError:
            pkg = {}
        pkg = pkg if isinstance(pkg, dict) else {}
    reqs = read("requirements.txt") or "" if "requirements.txt" in top else ""
    framework = detect_framework(pkg, top, reqs)
    if framework == "flask" and "Dockerfile" not in top:
        # detect_framework's Python fallback: only a web app when something serves a page.
        names = _requirement_names(reqs)
        app_py = (read("app.py") or "") if "app.py" in top else ""
        if not names & PY_WEB:
            # No web framework in requirements.txt (or no requirements.txt): the code's imports decide.
            for name in [f for f in ("app.py", "main.py", "streamlit_app.py") if f in top][:3]:
                text = app_py if name == "app.py" else (read(name) or "")
                for fw in ("streamlit", "gradio", "dash"):
                    if _ENTRY_MARK[fw].search(text):
                        return fw
        if not names & PY_WEB and not any(w in app_py for w in ("flask", "Flask", "fastapi", "FastAPI")):
            return "docs" if _previewable(files) else None
    if framework == "static":
        if _has_site(files):
            return "static"
        return "docs" if _previewable(files) else None
    return framework


def describe_non_web(files) -> str:
    """'12 Jupyter notebooks, 3 Terraform files and a README' style summary."""
    counts = [("Jupyter notebook", sum(f.endswith(".ipynb") for f in files)),
              ("Terraform file", sum(f.endswith((".tf", ".tfvars")) for f in files)),
              ("CI/CD workflow", sum(f.startswith(".github/workflows/") or f in (".gitlab-ci.yml", "Jenkinsfile")
                                     for f in files)),
              ("Ansible/YAML file", sum(f.endswith((".yml", ".yaml")) and not f.startswith(".github/") for f in files)),
              ("Python script", sum(f.endswith(".py") for f in files)),
              ("SQL file", sum(f.endswith(".sql") for f in files)),
              ("data file", sum(f.endswith((".csv", ".xlsx", ".parquet", ".json")) for f in files)),
              ("document", sum(f.endswith((".md", ".pdf", ".docx", ".pptx")) for f in files))]
    parts = [f"{n} {what}{'s' if n != 1 else ''}" for what, n in counts if n]
    return ", ".join(parts[:5]) if parts else "no source files"


def refusal(files) -> str:
    """The message when a project has nothing to run or preview."""
    return (f"This project has no web application to run (it contains {describe_non_web(files)}). Codeskys "
            "publishes websites, web apps and APIs; there is no page to show here. If the app lives in a folder, "
            "set the Root directory in Project → Settings.")


def unsafe_reason(files: set, read) -> str:
    """Why the project must never be deployed (intentionally vulnerable labs, host access), or ""."""
    top = top_level(files)
    compose = multiservice.find_compose(top)
    if compose:
        text = read(compose)
        if text:
            try:
                multiservice.plan(multiservice.parse(text), None)
            except multiservice.ComposePlanError as exc:
                msg = str(exc)
                if "vulnerable" in msg or "access to the server" in msg or "Docker socket" in msg:
                    return msg
    if "package.json" in top:
        try:
            name = str((json.loads(read("package.json") or "{}") or {}).get("name") or "")
        except (ValueError, AttributeError):
            name = ""
        if multiservice.VULNERABLE.search(name):
            return str(multiservice.lab_refusal(f"This project is {name[:80]}", ", "))
    if "Dockerfile" in top:
        from .buildpolicy import dockerfile_problems
        problems = [p for p in dockerfile_problems(read("Dockerfile") or "FROM scratch") if "vulnerable" in p]
        if problems:
            return "The Dockerfile " + problems[0] + "."
    return ""


# Python presets whose start command names one file: what that file contains, and the common names.
_ENTRY_MARK = {"streamlit": re.compile(r"^\s*(?:import streamlit|from streamlit\b)", re.M),
               "gradio": re.compile(r"^\s*(?:import gradio|from gradio\b)", re.M),
               "dash": re.compile(r"^\s*(?:import dash|from dash\b)", re.M),
               "fastapi": re.compile(r"^(\w+)\s*=\s*FastAPI\(", re.M),
               "flask": re.compile(r"^(\w+)\s*=\s*Flask\(", re.M)}
_ENTRY_ORDER = ("app.py", "main.py", "streamlit_app.py", "Home.py", "dashboard.py", "server.py", "run.py",
                "application.py", "api.py", "wsgi.py")


def entry_start(framework: str, files, read) -> str | None:
    """A start command for the file that really holds the app, when the preset's default file is
    missing or is not that app (streamlit_app.py, main.py for Flask, app.py for FastAPI...). None: keep
    the preset. Only top-level files are considered; ``read`` returns a small file's text."""
    from .dockerfiles import settings
    mark = _ENTRY_MARK.get(framework)
    if mark is None:
        return None
    preset = settings({"framework": framework})["start"]
    top = sorted(f for f in top_level(files) if f.endswith(".py"))
    ordered = [f for f in _ENTRY_ORDER if f in top] + [f for f in top if f not in _ENTRY_ORDER]
    for name in ordered[:12]:
        m = mark.search(read(name) or "")
        if not m:
            continue
        module = name[:-3]
        if framework == "streamlit":
            start = f"streamlit run {name} --server.port 8000 --server.address 0.0.0.0 --server.headless true"
        elif framework in ("gradio", "dash"):
            start = f"python {name}"
        elif not module.isidentifier():
            return None
        elif framework == "fastapi":
            start = f"uvicorn {module}:{m.group(1)} --host 0.0.0.0 --port 8000"
        else:
            start = f"gunicorn {module}:{m.group(1)} -b 0.0.0.0:8000"
        return None if start == preset else start
    return None


_MACHINE_PATH = re.compile(r"""["'](?:[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+[^"'\\/]+|/(?:Users|home)/[A-Za-z0-9._-]+/)[^"']{0,120}""")


def machine_paths(files, read, limit: int = 8) -> list:
    """[(file, path)] where top-level source code names a file on the student's own computer
    (C:\\Users\\..., /home/<name>/...): it does not exist in the deployed app."""
    found = []
    for name in sorted(f for f in top_level(files) if f.endswith((".py", ".js", ".mjs", ".ts")))[:limit]:
        text = read(name) or ""
        m = _MACHINE_PATH.search(text)
        if m:
            found.append((name, m.group(0)[1:80]))
    return found


def nested_root(files: set) -> str:
    """A folder (up to two levels down) that holds a runnable project, or ""."""
    best = ""
    for f in sorted(files, key=lambda p: (p.count("/"), p)):
        parts = f.split("/")
        if 2 <= len(parts) <= 3 and parts[-1] in MARKERS + ("index.html",):
            folder = "/".join(parts[:-1])
            if not any(seg in ("node_modules", ".git", "test", "tests", "examples", "docs") for seg in parts[:-1]):
                best = folder
                break
    return best


def detect(files: set, read) -> tuple:
    """(preset, root folder) of the project: the top level, or a folder up to two levels down when
    the top holds nothing runnable (only a README, notebooks, ...). (None, "") when nothing runs."""
    framework = choose(files, read)
    if framework not in (None, "docs"):
        return framework, ""
    sub = nested_root(files)
    if sub:
        inner = choose({f[len(sub) + 1:] for f in files if f.startswith(sub + "/")}, lambda p: read(f"{sub}/{p}"))
        if inner and inner != "docs":
            return inner, sub
    return framework, ""


def classify(files: set, read) -> dict:
    """{"category", "framework", "detail"} for the repository review.

    Categories: ready, configure, multi, non_web, unsafe, empty."""
    from .buildpolicy import entrypoint_problem
    from .dockerfiles import DockerfileError, settings
    if not files:
        return {"category": "empty", "framework": None, "detail": "the repository has no files"}
    unsafe = unsafe_reason(files, read)
    if unsafe:
        return {"category": "unsafe", "framework": None, "detail": unsafe}
    framework, sub = detect(files, read)
    if sub:
        # The app is in a folder: Codeskys sets the Root directory itself (engine._autodetect).
        inner = classify({f[len(sub) + 1:] for f in files if f.startswith(sub + "/")}, lambda p: read(f"{sub}/{p}"))
        return inner | {"root_dir": sub, "detail": f"in {sub}/: {inner['detail']}"}
    if framework is None:
        return {"category": "non_web", "framework": None, "detail": describe_non_web(files)}
    if framework == "docs":
        return {"category": "non_web", "framework": "docs",
                "detail": f"{describe_non_web(files)} (a notebook/README preview can be published)"}
    if framework == "compose":
        try:
            plan = multiservice.plan(multiservice.parse(read(multiservice.find_compose(top_level(files))) or ""), files)
        except multiservice.ComposePlanError as exc:
            return {"category": "configure", "framework": "compose", "detail": str(exc)}
        names = ", ".join(f"{s['name']}" + (f" ({s['image']})" if s["kind"] == "data" else "")
                          for s in plan["services"])
        detail = f"services: {names}; public port {plan['port']}"
        unknown = [k for k in plan["required_db_env"] if k not in plan["db_credentials"]]
        if unknown:
            return {"category": "configure", "framework": "compose",
                    "detail": f"{detail}; needs environment variables {', '.join(unknown[:8])}"}
        if plan["db_credentials"]:
            detail += f"; Codeskys prepares {', '.join(sorted(plan['db_credentials'])[:8])}"
        return {"category": "multi", "framework": "compose", "detail": detail}
    if framework == "docker":
        return {"category": "ready", "framework": "docker", "detail": "builds from its own Dockerfile"}
    project = {"framework": framework, "install_command": "", "build_command": "",
               "start_command": entry_start(framework, files, read) or "",
               "output_dir": "", "port": None, "node_version": "20", "root_dir": ""}
    try:
        s = settings(project)
    except DockerfileError as exc:
        return {"category": "configure", "framework": framework, "detail": str(exc)}
    problem = entrypoint_problem(project, s | {"label": FRAMEWORKS[framework]["name"]}, files)
    if problem:
        return {"category": "configure", "framework": framework, "detail": problem}
    return {"category": "ready", "framework": framework, "detail": f"{FRAMEWORKS[framework]['name']} preset"}
