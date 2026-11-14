"""Build isolation policy for Coolify deployments.

Coolify's own build packs (nixpacks, dockerfile, static, railpack) run
``docker build --network host`` (Coolify v4.7.5, ApplicationDeploymentJob): a
student's RUN step would execute in the server's network namespace and reach
every loopback-only service and every container IP. So Codeskys uses only a
Coolify *service* (Services API) made from a compose file it generates itself
(:mod:`composegen`) whose build section carries no network, entitlements or
privileges. BuildKit then refuses ``RUN --network=host`` and
``RUN --security=insecure``, also from a base image's ONBUILD, a custom
``# syntax`` frontend or a line continuation (tested 2026-11-12, Docker 29.8,
Compose 5.5; re-test on the server on install day).

This module adds the checks around that:

* :func:`dockerfile_problems`: the student's Dockerfile is refused early when it
  asks for host networking, insecure mode or devices, or changes how it is
  parsed. Defense in depth with a clear message; BuildKit is the real control,
  because a base image can still carry such instructions.
* :func:`coolify_service_problems`: before every build or start, the Coolify
  service must be a custom compose service (no template), not joined to the
  destination network, with exactly one container named ``app`` and, when
  Coolify shows it, exactly the compose file Codeskys generated.
"""
from __future__ import annotations

import json
import re

import requests

from .composegen import _GITHUB, _SHA

MAX_DOCKERFILE_BYTES = 256 * 1024

# Parser directives must come first; "syntax" loads a custom frontend image and
# "escape" changes the continuation character this check relies on.
_DIRECTIVE = re.compile(r"^\s*#\s*(syntax|escape|check)\s*=", re.I)
# Instruction flags that request host networking, insecure mode or host devices.
_FLAG = re.compile(r"--(network|security|device)\b", re.I)
# Line continuation, including comment lines Docker drops inside a continued instruction.
_CONTINUATION = re.compile(r"\\[ \t]*\r?\n(?:[ \t]*#[^\n]*\n)*")
_FROM = re.compile(r"^\s*FROM\s+\S", re.I | re.M)
_DOCKERFILE_PATH = re.compile(r"^(?:[A-Za-z0-9._-]+/)*Dockerfile$")



class PolicyError(ValueError):
    """The deployment would break build isolation. The message is safe to show."""


def dockerfile_problems(text: str) -> list[str]:
    problems = []
    for line in text.splitlines():
        if not line.strip().startswith("#"):
            break
        m = _DIRECTIVE.match(line)
        if m:
            problems.append(f"the '# {m.group(1).lower()}=' parser directive is not allowed")
    joined = _CONTINUATION.sub("", text)
    for line in joined.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _FLAG.search(s)
        if m:
            problems.append(f"'{m.group(0)}' is not allowed (in: {s[:80]})")
    if not _FROM.search(joined):
        problems.append("no FROM instruction (is the Dockerfile a symlink?)")
    return problems


def fetch_dockerfile(repo_url: str, commit_sha: str, path: str = "Dockerfile", timeout: float = 15.0) -> str:
    """The Dockerfile exactly as the build will see it: same repository, same commit."""
    m = _GITHUB.match((repo_url or "").rstrip("/").removesuffix(".git"))
    if not m or not _SHA.match(commit_sha or ""):
        raise PolicyError("Only public GitHub repositories at an exact commit can be deployed.")
    if not _DOCKERFILE_PATH.match(path or "") or ".." in path.split("/"):
        raise PolicyError("The root directory must be a relative path inside the repository.")
    url = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{commit_sha}/{path}"
    try:
        r = requests.get(url, timeout=timeout, stream=True, allow_redirects=False)
    except requests.RequestException:
        raise PolicyError("Could not read the Dockerfile from GitHub. Try again in a moment.") from None
    with r:
        if r.status_code == 404:
            raise PolicyError(f"The repository has no {path} at its root for this commit. "
                              "Deployments are built from your own Dockerfile.")
        if r.status_code != 200:
            raise PolicyError(f"Could not read the Dockerfile from GitHub (HTTP {r.status_code}). Try again.")
        data = r.raw.read(MAX_DOCKERFILE_BYTES + 1, decode_content=True)
    if len(data) > MAX_DOCKERFILE_BYTES:
        raise PolicyError("The Dockerfile is larger than 256 KB.")
    return data.decode("utf-8", errors="replace")


def check_dockerfile(repo_url: str, commit_sha: str, path: str = "Dockerfile") -> None:
    problems = dockerfile_problems(fetch_dockerfile(repo_url, commit_sha, path))
    if problems:
        raise PolicyError("This Dockerfile cannot be built on Codeskys: " + "; ".join(problems) + ".")


def coolify_service_problems(service: dict, expected_compose: dict | None = None) -> list[str]:
    """Empty when the Coolify service is safe to build/start (fields of GET
    /services/{uuid}, Coolify 4.7.5).

    ``docker_compose_raw`` is only in the answer for a token with read:sensitive,
    which the Codeskys token does not have; then the stored file cannot be compared
    and Codeskys relies on having written it itself (PATCH right before every
    deploy, token scoped to the Codeskys project). When it is visible it must equal
    ``expected_compose`` (deploy) or at least pass :func:`composegen.assert_safe_service`.
    """
    from .composegen import assert_safe_service
    problems = []
    if service.get("service_type"):
        problems.append(f"it is a template service ({service.get('service_type')!r})")
    if service.get("connect_to_docker_network"):
        problems.append("it is connected to the destination's Docker network")
    names = sorted(a.get("name") for a in service.get("applications") or [] if isinstance(a, dict))
    if names != ["app"] or service.get("databases"):
        problems.append(f"its containers are {names + ['(database)'] * len(service.get('databases') or [])}, "
                        "not one 'app'")
    if "docker_compose_raw" in service:
        try:
            stored = json.loads(service.get("docker_compose_raw") or "")
            assert_safe_service(stored)
        except (TypeError, ValueError):        # ComposeError is a ValueError
            problems.append("its compose file is not a safe Codeskys compose file")
            return problems
        if expected_compose is not None and stored != expected_compose:
            problems.append("its compose file is not the one Codeskys generated")
    return problems


# ---------------------------------------------------------------- entry-point preflight
# A framework preset assumes a layout (``gunicorn app:app`` needs app.py). When the
# repository does not have it, the build would run for minutes and the app would only
# crash at start. Checked against the exact commit before Coolify is asked to build.

_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_APP_REF = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*):[A-Za-z_][A-Za-z0-9_]*(?:\(.*\))?$")
# Server options that take a value (so the value is not mistaken for the app module).
_VALUE_OPTS = {"-b", "--bind", "-w", "--workers", "-k", "--worker-class", "-t", "--timeout", "--host",
               "--port", "--threads", "-c", "--config", "--log-level", "--root-path", "--env-file",
               "--name", "-n", "--pythonpath", "--uds", "--fd", "--loop", "--http", "--ws", "--lifespan",
               "--interface", "--workers-per-core"}
_BASE_DIR_OPTS = {"--chdir", "--app-dir"}          # these move the base directory: not checked
ENTRY_NAMES = ("main.py", "app.py", "wsgi.py", "asgi.py", "server.py", "manage.py")


def _module_files(module: str) -> list[str]:
    path = module.replace(".", "/")
    return [f"{path}.py", f"{path}/__init__.py"]


def required_files(install: str, start: str) -> list[tuple[str, list[str]]]:
    """``[(what, [paths, any one of which must exist])]`` that the commands rely on."""
    needs = []
    try:
        import shlex
        words = shlex.split(install or "")
    except ValueError:
        words = (install or "").split()
    for i, w in enumerate(words[:-1]):
        if w in ("-r", "--requirement"):
            needs.append((f"the install command's {words[i + 1]}", [words[i + 1]]))
    if words[:2] in (["npm", "install"], ["npm", "ci"]):
        needs.append(("package.json (for npm)", ["package.json"]))
    try:
        import shlex
        tokens = shlex.split(start or "")
    except ValueError:
        return needs
    if not tokens or any(t.split("=", 1)[0] in _BASE_DIR_OPTS for t in tokens):
        return needs
    if tokens[0] == "exec":
        tokens = tokens[1:]
    cmd = tokens[0] if tokens else ""
    if cmd in ("gunicorn", "uvicorn"):
        skip = False
        for tok in tokens[1:]:
            if skip:
                skip = False
                continue
            if tok.startswith("-"):
                skip = tok in _VALUE_OPTS
                continue
            m = _APP_REF.match(tok)
            module = m.group(1) if m else (tok if cmd == "gunicorn" and _MODULE.match(tok) else "")
            if module:
                needs.append((f"the module {module} that the start command runs", _module_files(module)))
            break
    elif cmd in ("python", "python3") and len(tokens) > 1 and tokens[1].endswith(".py"):
        needs.append((tokens[1], [tokens[1]]))
    elif cmd == "streamlit" and len(tokens) > 2 and tokens[1] == "run":
        needs.append((tokens[2], [tokens[2]]))
    return needs


def repository_files(repo_url: str, commit_sha: str, timeout: float = 10.0):
    """Every file path at the commit, or None when GitHub cannot tell (never blocks a build)."""
    m = _GITHUB.match((repo_url or "").rstrip("/").removesuffix(".git"))
    if not m or not _SHA.match(commit_sha or ""):
        return None
    try:
        r = requests.get(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/git/trees/{commit_sha}",
                         params={"recursive": "1"}, timeout=timeout, allow_redirects=False,
                         headers={"Accept": "application/vnd.github+json", "User-Agent": "Codeskys-Deploy"})
        if r.status_code != 200:
            return None
        body = r.json()
    except (requests.RequestException, ValueError):
        return None
    if body.get("truncated"):
        return None
    return {e["path"] for e in body.get("tree", []) if e.get("type") == "blob" and isinstance(e.get("path"), str)}


def entrypoint_problem(project, settings: dict, files) -> str:
    """Why the preset's commands cannot work in this repository, or "" (unknown counts as fine)."""
    if files is None:
        return ""
    root = (project["root_dir"] or "").strip("/")
    under = (lambda p: f"{root}/{p}" if root else p)
    rel = {f[len(root) + 1:] if root else f for f in files if not root or f.startswith(root + "/")}
    missing = [what for what, paths in required_files(settings.get("install", ""), settings.get("start", ""))
               if not any(p in rel for p in paths)]
    if not missing:
        return ""
    where = f"the {root}/ folder" if root else "the top level of the repository"
    msg = [f"This repository does not match the {settings.get('label') or 'selected'} preset: "
           f"{', '.join(missing)} is not in {where}, so the app could not start."]
    entries = sorted(p for p in rel if p.count("/") <= 2 and p.rsplit("/", 1)[-1] in ENTRY_NAMES
                     and not p.startswith(("tests/", "test/")))[:5]
    if entries:
        msg.append("Possible entry points: " + ", ".join(under(e) for e in entries) + ".")
    compose = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"} & rel
    dockerfiles = [p for p in rel if "/" not in p and p.startswith("Dockerfile.")]
    if compose and (dockerfiles or len(entries) > 1):
        msg.append("It is a multi-service Docker Compose project; a Codeskys project runs one service.")
    example = _example_start(settings.get("start", ""), entries)
    msg.append("Set the Install command, Start command and Port in Project → Settings for the part you want "
               "to run" + (f" (for example: {example})" if example else "") + ", then deploy again.")
    return " ".join(msg)


def _example_start(start: str, entries) -> str:
    """The preset's own server pointed at the first entry point found, e.g. uvicorn backend.main:app."""
    server = (start or "").split(maxsplit=1)[0] if start else ""
    py = [e for e in entries if e.endswith(".py") and not e.endswith("manage.py")]
    if server not in ("gunicorn", "uvicorn") or not py:
        return ""
    module = py[0][:-3].replace("/", ".")
    # Only file names are known here, not whether the app is WSGI (Flask) or ASGI (FastAPI).
    return (f"uvicorn {module}:app --host 0.0.0.0 --port 8000 for a FastAPI app, "
            f"or gunicorn {module}:app -b 0.0.0.0:8000 for a Flask app")

