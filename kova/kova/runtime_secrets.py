"""Hand-off of deployment-bound runtime secrets to the runtime-proxy sidecar.

authz returns a deployment's ``runtime_secret`` exactly once. The contract
(forgeops/authz/WEBSITE_INTEGRATION.md) allows exactly one destination: a secret
FILE read by that deployment's runtime proxy. So the website:

* writes it to ``<CODESKYS_RUNTIME_SECRET_DIR>/<dep_id>/runtime_secret`` with mode
  0600 inside a 0700 directory (atomic rename, never partially written);
* never stores it in its database, never logs it, never puts it in a Coolify
  environment variable (those are visible to build steps and to the app);
* deletes the file when the deployment is revoked, expires or is superseded.

The directory is mounted into the runtime-proxy sidecar of the matching Coolify
application only (see docs/COOLIFY_INSTALL_PLAN.md). Without a configured
directory nothing is requested from authz, so no credential exists to leak.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from pathlib import Path

_DEP = re.compile(r"^dep_[A-Za-z0-9]{6,28}$")

# Groups that guard platform secrets on the VPS (forgeops ops/vps-http/provision.sh):
# 0 root, 1996 codeskys-coolifygw (Coolify API token), 1998 codeskys-ghkey (GitHub App
# private key), 1999 codeskys-runtime (the platform's own runtime secret). Runtime
# secret readers must never be given one of them.
PLATFORM_SECRET_GIDS = frozenset({0, 1996, 1998, 1999})


class SinkError(RuntimeError):
    pass


def enabled(app_config) -> bool:
    return bool(app_config.get("RUNTIME_SECRET_DIR"))


def _dir(app_config, dep_id: str) -> Path:
    if not _DEP.match(dep_id or ""):
        raise SinkError("unexpected deployment id format")
    base = Path(app_config["RUNTIME_SECRET_DIR"]).resolve()
    target = (base / dep_id).resolve()
    if target.parent != base:
        raise SinkError("deployment directory escapes the secret root")
    return target


def _gid(app_config):
    gid = app_config.get("RUNTIME_SECRET_GID")
    if gid in (None, ""):
        return None
    gid = int(gid)
    if not 1 <= gid <= 65535:
        raise SinkError("invalid runtime secret group id")
    if gid in PLATFORM_SECRET_GIDS:
        raise SinkError("runtime secret group id is reserved for platform secrets")
    return gid


def deliver(app_config, dep_id: str, secret: str) -> str:
    """Write the secret file. Returns its path (not the secret).

    Without ``RUNTIME_SECRET_GID``: directory 0700, file 0600 (owner only).
    With it: directory 0750 and file 0640, group = that gid. The sidecar runs as a
    different uid and gets ONLY that supplementary group (compose ``group_add``),
    the same member-less-group pattern the VPS uses for its other secret files.
    """
    if not secret:
        raise SinkError("empty runtime secret")
    target = _dir(app_config, dep_id)
    gid = _gid(app_config)
    old = os.umask(0o077)
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = target / ".runtime_secret.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, secret.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        if gid is not None and hasattr(os, "chown"):
            os.chown(tmp, -1, gid)
            os.chmod(tmp, 0o640)
            os.chown(target, -1, gid)
            os.chmod(target, 0o750)
        final = target / "runtime_secret"
        os.replace(tmp, final)
    except OSError as exc:
        raise SinkError(f"could not write the runtime secret file ({exc.__class__.__name__})") from None
    finally:
        os.umask(old)
    return str(final)


_SERVICE_UUID = re.compile(r"^[a-z0-9]{8,64}$")
WORKLOAD_TOKEN = re.compile(r"^cwt_[A-Za-z0-9_-]{43}$")


def new_workload_token() -> tuple[str, str]:
    """(token, sha256 hex) for one deployment. The token goes to the app as
    CODESKYS_RUNTIME_TOKEN; only the hash is written to disk. It proves nothing
    on its own: the gateway also requires the app's own network position."""
    token = "cwt_" + secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def gateway_enabled(app_config) -> bool:
    """Coolify deployments get a credential only when the shared runtime gateway
    (forgeops runtime/gateway.py) reads this secret store."""
    return enabled(app_config) and bool(app_config.get("RUNTIME_GATEWAY_URL"))


def bind(app_config, dep_id: str, service_uuid: str, github_repo_id: int, workload_token_sha256: str) -> str:
    """Tell the runtime gateway which Coolify service runs this deployment.

    ``binding.json`` (not secret) sits next to the deployment's secret file. The
    gateway serves a calling app only when its address belongs to that service
    (codeskys-route-sync's map) AND exactly one deployment is bound to it.
    """
    if not _SERVICE_UUID.match(service_uuid or ""):
        raise SinkError("unexpected service id format")
    if isinstance(github_repo_id, bool) or not isinstance(github_repo_id, int) or github_repo_id < 1:
        raise SinkError("invalid repository id")
    if not re.match(r"^[0-9a-f]{64}$", workload_token_sha256 or ""):
        raise SinkError("invalid workload token hash")
    target = _dir(app_config, dep_id)
    if not (target / "runtime_secret").is_file():
        raise SinkError("the deployment has no runtime secret")
    gid = _gid(app_config)
    body = json.dumps({"service_uuid": service_uuid, "deployment_id": dep_id, "github_repo_id": github_repo_id,
                       "workload_token_sha256": workload_token_sha256}).encode("utf-8")
    try:
        tmp = target / ".binding.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        if gid is not None and hasattr(os, "chown"):
            os.chown(tmp, -1, gid)
            os.chmod(tmp, 0o640)
        final = target / "binding.json"
        os.replace(tmp, final)
    except OSError as exc:
        raise SinkError(f"could not write the deployment binding ({exc.__class__.__name__})") from None
    return str(final)


def discard(app_config, dep_id: str) -> bool:
    """Remove a deployment's secret file. Idempotent; True if something was removed."""
    if not enabled(app_config) or not dep_id:
        return False
    try:
        target = _dir(app_config, dep_id)
    except SinkError:
        return False
    if not target.exists():
        return False
    secret = target / "runtime_secret"
    try:
        if secret.exists():  # overwrite before unlinking
            size = secret.stat().st_size
            with open(secret, "r+b") as fh:
                fh.write(b"\0" * size)
                fh.flush()
                os.fsync(fh.fileno())
        shutil.rmtree(target)
    except OSError:
        return False
    return True
