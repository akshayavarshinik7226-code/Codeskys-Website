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
