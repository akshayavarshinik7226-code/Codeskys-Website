"""Deployment engine: the state machine and its two providers.

    queued ──▶ building ──┬──▶ ready ──▶ expired (after the project's TTL)
                          ├──▶ failed
                          └──▶ cancelled

Work is driven by the durable queue in :mod:`kova.jobs`, so a deployment survives
a restart. Two providers implement the same contract:

``simulator``
    Runs the pipeline locally and writes realistic logs **without building or
    running any repository code**. Nothing it produces is ever labelled a real
    deployment: rows carry ``provider='simulator'`` and the UI badges them.

``coolify``
    Creates and drives a real Coolify *service* (one per project) over its v1 API.

Cancellation crosses process boundaries through the database: the API sets
``stage='cancelling'`` and the worker notices on its next checkpoint.
"""
import json
import random
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests

from . import authz_client, buildpolicy, runtime_secrets
from .db import get_setting, utcnow
from .crypto import Secrets
from .security import redact
from .gitinfo import FRAMEWORKS, latest_commit
from .jobs import enqueue, handler

SIMULATOR = "simulator"
COOLIFY = "coolify"


class CoolifyError(RuntimeError):
    """A Coolify call failed. Always surfaced to the user — never swallowed.

    ``str(exc)`` is safe for students; ``detail`` keeps the unredacted upstream text
    for administrators.
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail or message


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- settings

def coolify_settings(conn, app_config) -> dict:
    """Environment variables win; the Integrations screen is the fallback store."""
    return {
        "url": (app_config.get("COOLIFY_URL") or get_setting(conn, "coolify_url", "") or "").rstrip("/"),
        "token": app_config.get("COOLIFY_TOKEN") or get_setting(conn, "coolify_token", "") or "",
        "project_uuid": app_config.get("COOLIFY_PROJECT_UUID") or get_setting(conn, "coolify_project_uuid", "") or "",
        "server_uuid": app_config.get("COOLIFY_SERVER_UUID") or get_setting(conn, "coolify_server_uuid", "") or "",
        "environment_name": get_setting(conn, "coolify_environment", "production") or "production",
        "base_domain": (app_config.get("COOLIFY_BASE_DOMAIN") or get_setting(conn, "coolify_base_domain", "") or ""),
        "private_key_uuid": app_config.get("COOLIFY_PRIVATE_KEY_UUID") or get_setting(conn, "coolify_private_key_uuid", "") or "",
        "github_app_uuid": app_config.get("COOLIFY_GITHUB_APP_UUID") or get_setting(conn, "coolify_github_app_uuid", "") or "",
        # Dedicated Docker network "destination" for student apps, so they share no
        # network with Coolify's own database, Redis or API.
        "destination_uuid": app_config.get("COOLIFY_DESTINATION_UUID") or get_setting(conn, "coolify_destination_uuid", "") or "",
        "force_https": _force_https(conn, app_config),
        "tls_at_edge": bool(app_config.get("COOLIFY_TLS_AT_EDGE")) or get_setting(conn, "coolify_tls_at_edge", "") == "1",
        # Readiness check after a build: how long to wait, and where to send the HTTP
        # probe from inside the server (Traefik), instead of the public URL.
        "ready_timeout": int(app_config.get("COOLIFY_READY_TIMEOUT") or 180),
        "probe_url": (app_config.get("COOLIFY_PROBE_URL") or "").rstrip("/"),
    }


def app_domains(slug: str, settings) -> tuple:
    """(domain given to Coolify, public URL shown to the student), or (None, None).

    With TLS at the edge, Caddy holds the certificate and forwards plain HTTP to
    Traefik: Coolify must get an http:// domain (an https:// one makes Traefik
    request its own certificate and redirect to HTTPS, which loops behind Caddy).
    """
    base = (settings.get("base_domain") or "").strip().lower().strip(".")
    if not base:
        return None, None
    host = f"{slug}.{base}"
    if settings.get("tls_at_edge"):
        return f"http://{host}", f"https://{host}"
    scheme = "https" if settings.get("force_https", True) else "http"
    return f"{scheme}://{host}", f"{scheme}://{host}"


def _force_https(conn, app_config) -> bool:
    stored = get_setting(conn, "coolify_force_https", "")
    if stored in ("0", "1"):
        return stored == "1"
    return bool(app_config.get("COOLIFY_FORCE_HTTPS", True))


def coolify_ready(settings) -> bool:
    return bool(settings["url"] and settings["token"] and settings["project_uuid"] and settings["server_uuid"])


def active_provider(conn, app_config) -> str:
    """Coolify only when it is both selected and completely configured.

    A half-configured Coolify never silently becomes a real deployment, and never
    strands a build either — it falls back to the simulator, which the UI labels.
    """
    if get_setting(conn, "deploy_provider", SIMULATOR) == COOLIFY and coolify_ready(coolify_settings(conn, app_config)):
        return COOLIFY
    return SIMULATOR


# ---------------------------------------------------------------- Coolify client

class Coolify:
    """Thin client over the Coolify v1 API. The token never leaves the backend."""

    def __init__(self, url: str, token: str, timeout: float = 20.0):
        self.base = url.rstrip("/") + "/api/v1"
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                        "Content-Type": "application/json"}

    def _req(self, method, path, **kw):
        try:
            r = requests.request(method, self.base + path, headers=self.headers, timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise CoolifyError(f"Could not reach the deployment engine: {exc.__class__.__name__}",
                               f"Could not reach Coolify at {self.base}: {exc}") from exc
        if r.status_code == 401:
            raise CoolifyError("The deployment engine rejected the API token (401). Check the token and its permissions.")
        if r.status_code == 403:
            raise CoolifyError("The deployment engine denied this action (403). The token is missing a required permission.")
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message") or body.get("error") or json.dumps(body)[:300]
            except ValueError:
                msg = r.text[:300]
            raise CoolifyError(f"The deployment engine returned {r.status_code}: {redact(msg)}",
                               f"Coolify returned {r.status_code}: {msg}")
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}

    # -- discovery
    def version(self):
        return self._req("GET", "/version")

    def servers(self):
        return self._req("GET", "/servers")

    def projects(self):
        return self._req("GET", "/projects")

    def private_keys(self):
        return self._req("GET", "/security/keys")

    # -- the project's app = one Coolify *service* (Services API, Coolify 4.7.5)
    #
    # 4.7.5 has no endpoint that creates an *application* from a raw compose file, and
    # its git/Dockerfile application build packs build with --network host. A service
    # made from docker_compose_raw is started with `docker compose up -d --build` on
    # exactly that file, which keeps the build isolation of the generated compose.
    # Paths, fields and abilities checked against openapi.yaml, routes/api.php and
    # ServicesController.php at the v4.7.5 tag (COOLIFY_SINGLE_VPS_PLAN.md §14).
    # Start, stop, restart and delete only queue work and return a message: progress
    # is read from the service's ``status`` ("starting:…" while the build runs).

    def services(self):
        return self._req("GET", "/services")

    def create_service(self, body: dict) -> str:
        return self._req("POST", "/services", json=body)["uuid"]

    def update_service(self, uuid: str, body: dict):
        return self._req("PATCH", f"/services/{uuid}", json=body)

    def get_app(self, uuid: str):
        return self._req("GET", f"/services/{uuid}")

    def bulk_envs(self, uuid: str, envs: list):
        if envs:
            return self._req("PATCH", f"/services/{uuid}/envs/bulk", json={"data": envs})

    def app_logs(self, uuid: str, lines: int = 200):
        """Output of the ``app`` container. Needs the read:sensitive ability, which the
        Codeskys token does not have: callers treat a refusal as "no logs"."""
        return self._req("GET", f"/services/{uuid}/logs", params={"sub_service_name": "app", "lines": lines})

    def stop_app(self, uuid: str):
        try:
            return self._req("POST", f"/services/{uuid}/stop", params={"docker_cleanup": "true"})
        except CoolifyError as exc:
            if "already stopped" in str(exc).lower():
                return {"message": "already stopped"}
            raise

    def start_app(self, uuid: str):
        """Builds and starts. Coolify refuses (400) a service that is already running."""
        return self._req("POST", f"/services/{uuid}/start")

    def restart_app(self, uuid: str):
        """Stops, then builds and starts again with the stored compose file."""
        return self._req("POST", f"/services/{uuid}/restart")

    def run_app(self, uuid: str):
        """Build and start the stored compose file: start, or restart when it is running."""
        try:
            return self.start_app(uuid)
        except CoolifyError as exc:
            if "already running" not in str(exc).lower():
                raise
        return self.restart_app(uuid)

    def delete_app(self, uuid: str):
        # delete_connected_networks removes only the service's own network (named after
        # its uuid), never the external codeskys-* networks the compose file joins.
        return self._req("DELETE", f"/services/{uuid}", params={
            "delete_configurations": "true", "delete_volumes": "true",
            "docker_cleanup": "true", "delete_connected_networks": "true"})


def client_for(conn, app_config):
    s = coolify_settings(conn, app_config)
    if not (s["url"] and s["token"]):
        raise CoolifyError("The deployment engine is not configured. An administrator must connect it in Integrations.")
    return Coolify(s["url"], s["token"]), s


# ---------------------------------------------------------------- db helpers

def _log(conn, dep_id, line, level="info", stage=""):
    conn.execute("INSERT INTO deploy_logs (deployment_id, ts, level, stage, line) VALUES (?,?,?,?,?)",
                 (dep_id, utcnow(), level, stage, line[:4000]))
    conn.commit()


def _set(conn, dep_id, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE deployments SET {cols} WHERE id = ?", (*fields.values(), dep_id))
    conn.commit()


def _notify(conn, user_id, kind, title, body, link):
    if user_id:
        conn.execute("INSERT INTO notifications (user_id, kind, title, body, link, created_at) VALUES (?,?,?,?,?,?)",
                     (user_id, kind, title, body, link, utcnow()))
        conn.commit()


def _cancel_requested(conn, dep_id) -> bool:
    row = conn.execute("SELECT stage, status FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    return bool(row) and (row["stage"] == "cancelling" or row["status"] == "cancelled")


def request_cancel(conn, dep_id):
    """Signal a running build to stop. Crosses process boundaries via the database."""
    conn.execute("UPDATE deployments SET stage = 'cancelling' WHERE id = ? AND status IN ('queued','building')",
                 (dep_id,))
    conn.commit()


def _ms_since(iso):
    return int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() * 1000)


def _finish(conn, dep, project, status, url=None, error=None):
    started = dep["started_at"] or dep["created_at"]
    fields = {"status": status, "stage": status, "finished_at": utcnow(), "duration_ms": _ms_since(started)}
    if url:
        fields["url"] = url
    if error:
        fields["error"] = error[:500]
    if status == "ready":
        ttl = project["ttl_hours"] or 24
        fields["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=ttl)).isoformat(timespec="seconds")
    _set(conn, dep["id"], **fields)
    if status == "ready" and dep["target"] == "production":
        conn.execute("UPDATE projects SET current_deployment_id = ?, updated_at = ? WHERE id = ?",
                     (dep["id"], utcnow(), project["id"]))
        conn.commit()
    link = f"#/projects/{project['slug']}/deployments/{dep['id']}"
    sim = " (simulated)" if dep["provider"] == SIMULATOR else ""
    if status == "ready":
        _notify(conn, project["owner_id"], "deploy.ready", f"{project['name']} is live{sim}",
                f"Deploy #{dep['number']} published to {dep['target']}.", link)
    elif status == "failed":
        _notify(conn, project["owner_id"], "deploy.failed", f"{project['name']} failed to deploy",
                error or f"Deploy #{dep['number']} failed.", link)


# ---------------------------------------------------------------- producing work

def enqueue_deploy(conn, dep_id):
    return enqueue(conn, "deploy", {"deployment_id": dep_id}, dedupe_key=f"deploy:{dep_id}", max_attempts=3)


def resolve_commit(project, branch, token=None):
    """Real head commit when the provider is reachable, else a placeholder."""
    c = latest_commit(project["repo_full_name"], branch, token=token)
    if c:
        return c["sha"], c["message"]
    return secrets.token_hex(20), ""


# ---------------------------------------------------------------- authorization (authz)

class AuthorizationRefused(RuntimeError):
    """authz said no (or could not be asked). The message is safe to show students."""


# Non-secret identifiers a deployment's app may read. The runtime SECRET is never
# an environment variable (build steps and the app could read it): it goes only
# to the runtime-proxy sidecar as a file, via :mod:`kova.runtime_secrets`.
RUNTIME_ENV = ("CODESKYS_DEPLOYMENT_ID", "CODESKYS_REPOSITORY_ID", "CODESKYS_RUNTIME_SECRET",
               "CODESKYS_RUNTIME_URL")


def _col(row, name, default=None):
    return row[name] if name in row.keys() else default


def authorize_deployment(conn, app_config, dep, project, with_credential=True):
    """Verify the repository with authz and, when a runtime-secret sink exists and
    ``with_credential``, obtain a deployment-bound credential for this REAL build.

    Returns NON-SECRET Coolify env entries (possibly empty). Raises
    :class:`AuthorizationRefused` with a student-facing message when the build
    must not go ahead (suspended project, repository mismatch, ...).
    """
    client = authz_client.client_from_config(app_config)
    linked = bool(_col(project, "authz_project_id", ""))
    if client is None or not linked:
        if app_config.get("AUTHZ_REQUIRED"):
            raise AuthorizationRefused(
                "This project is not linked to Codeskys authorization yet. Link it in the project's "
                "Settings (or ask an administrator) before deploying.")
        why = ("Project is not linked to Codeskys authorization" if not linked
               else "Codeskys authorization is not configured on this server")
        _log(conn, dep["id"], f"{why}: the app will run, but protected Codeskys services will refuse it.",
             "warn", "provisioning")
        return []
    pid = project["authz_project_id"]
    try:
        state = client.get_project(pid)
        if not state.get("can_deploy", True):
            raise authz_client.AuthzError(403, str(state.get("code") or authz_client.POLICY))
        repo_id = project["github_repo_id"]
        if not repo_id:
            reg = client.register_repository(pid, project["github_installation_id"])
            repo_id = int(reg["github_repo_id"])
            conn.execute("UPDATE projects SET github_repo_id = ? WHERE id = ?", (repo_id, project["id"]))
            conn.commit()
            _log(conn, dep["id"], f"Repository verified with GitHub: {reg.get('full_name')} (id {repo_id}, "
                                  f"{reg.get('verified_by')})", "success", "provisioning")
        if not with_credential:
            # authz checks the repository when it issues a credential; without one the
            # same rule is applied to the registered id in the project object.
            registered = (state.get("repository") or {}).get("github_repo_id")
            if project["github_repo_id"] and registered is not None and int(registered) != int(repo_id):
                raise authz_client.AuthzError(403, authz_client.REPO)
            _log(conn, dep["id"], "Repository authorized. Protected Codeskys services are not available to "
                                  "live deployments yet (the engine cannot keep a runtime sidecar's network "
                                  "away from the app), so no deployment credential was requested.",
                 "warn", "provisioning")
            return []
        if not runtime_secrets.enabled(app_config):
            _log(conn, dep["id"], "Repository authorized. Protected Codeskys services are not wired to this "
                                  "engine yet (no runtime-proxy sidecar), so no deployment credential was "
                                  "requested.", "warn", "provisioning")
            return []
        out = client.create_deployment(pid, repo_id, dep["commit_sha"])
    except authz_client.AuthzError as exc:
        # Operator detail (status/code/reference) goes to the build log; never secrets.
        _log(conn, dep["id"], f"Authorization refused: {exc.describe()}", "error", "provisioning")
        raise AuthorizationRefused(exc.user_message()) from None
    dep_ref = str(out.get("deployment_id") or "")[:32]
    secret = out.pop("runtime_secret", "") or ""
    _set(conn, dep["id"], authz_deployment_id=dep_ref)
    try:
        if not secret:
            raise runtime_secrets.SinkError("authz returned no runtime secret")
        runtime_secrets.deliver(app_config, dep_ref, secret)
    except runtime_secrets.SinkError as exc:
        del secret
        _log(conn, dep["id"], f"Could not hand the deployment credential to the runtime proxy: {exc}",
             "error", "provisioning")
        raise AuthorizationRefused("Codeskys could not prepare this deployment's credential. "
                                   "Try again; if it keeps failing, contact an administrator.") from None
    del secret
    # authz superseded the project's earlier deployments; drop their secret files.
    for prev in conn.execute("SELECT * FROM deployments WHERE project_id = ? AND id != ? "
                             "AND authz_deployment_id != '' AND authz_revoked_at IS NULL",
                             (project["id"], dep["id"])).fetchall():
        runtime_secrets.discard(app_config, prev["authz_deployment_id"])
        conn.execute("UPDATE deployments SET authz_revoked_at = ? WHERE id = ?", (utcnow(), prev["id"]))
    conn.commit()
    _log(conn, dep["id"], f"Deployment credential {dep_ref} issued (expires {out.get('expires_at', '?')}) "
                          "and handed to the runtime proxy as a secret file. It is not stored by the website.",
         "success", "provisioning")
    is_preview = dep["target"] == "preview"
    env = [{"key": "CODESKYS_DEPLOYMENT_ID", "value": dep_ref, "is_preview": is_preview},
           {"key": "CODESKYS_REPOSITORY_ID", "value": str(repo_id), "is_preview": is_preview}]
    if app_config.get("RUNTIME_URL"):
        env.append({"key": "CODESKYS_RUNTIME_URL", "value": app_config["RUNTIME_URL"], "is_preview": is_preview})
    return env


# A revocation that fails (authz unreachable) is retried by a durable job: every
# 5 minutes for ~25 hours, longer than authz's own backstop TTL
# (AUTHZ_DEPLOYMENT_TTL_DAYS=1), after which the credential is dead anyway.
REVOKE_RETRY_ATTEMPTS = 300


def schedule_revoke_retry(conn, dep_ref: str, deployment_id, reason: str):
    """Queue a durable revocation retry. Survives restarts and project deletion
    (the job carries the credential id itself, not just the deployment row)."""
    return enqueue(conn, "authz_revoke",
                   {"authz_deployment_id": dep_ref, "deployment_id": deployment_id, "reason": reason[:200]},
                   dedupe_key=f"authz-revoke:{dep_ref}", delay=15, max_attempts=REVOKE_RETRY_ATTEMPTS)


def _revoke_remote(app_config, dep_ref: str, reason: str):
    """Revoke at authz. Raises RuntimeError when authz did not confirm it."""
    client = authz_client.client_from_config(app_config)
    if client is None:
        raise RuntimeError("authz is not configured")
    try:
        client.revoke_deployment(dep_ref, reason)
    except authz_client.AuthzError as exc:
        if exc.status != 404:                  # 404 = already gone: nothing left to revoke
            raise RuntimeError(exc.describe()) from None


def revoke_authz_deployment(conn, app_config, dep, reason: str) -> str:
    """Revoke a deployment's credential. Idempotent; never raises.

    Local kill switch first: the sidecar's secret file is zeroed in place and
    removed, so this server stops using the credential at once, whether or not
    authz can be reached right now. Then authz is told; if that fails, a durable
    retry is queued.
    """
    dep_ref = _col(dep, "authz_deployment_id", "")
    if not dep_ref or _col(dep, "authz_revoked_at"):
        return ""
    runtime_secrets.discard(app_config, dep_ref)
    try:
        _revoke_remote(app_config, dep_ref, reason)
    except RuntimeError as exc:
        schedule_revoke_retry(conn, dep_ref, dep["id"], reason)
        return (f"Authorization credential {dep_ref} could not be revoked ({exc}); its secret file was "
                "destroyed and the revocation will be retried automatically.")
    conn.execute("UPDATE deployments SET authz_revoked_at = ? WHERE id = ?", (utcnow(), dep["id"]))
    conn.commit()
    return f"Authorization credential {dep_ref} revoked."


def revoke_project_credentials(conn, app_config, project_id, reason: str) -> list:
    """Revoke every unrevoked credential of a project (deletion, suspension)."""
    notes = []
    rows = conn.execute("SELECT * FROM deployments WHERE project_id = ? AND authz_deployment_id != '' "
                        "AND authz_revoked_at IS NULL", (project_id,)).fetchall()
    for d in rows:
        n = revoke_authz_deployment(conn, app_config, d, reason)
        if n:
            notes.append(n)
    return notes


# ---------------------------------------------------------------- job handlers

@handler("deploy")
def run_deployment(conn, app_config, payload, beat):
    dep_id = payload["deployment_id"]
    dep = conn.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    if dep is None or dep["status"] in ("ready", "failed", "cancelled", "expired"):
        return
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (dep["project_id"],)).fetchone()
    if project is None:
        return
    if _cancel_requested(conn, dep_id):
        _finish(conn, dep, project, "cancelled")
        return
    if not dep["started_at"]:
        _set(conn, dep_id, status="building", stage="initializing", started_at=utcnow())
    else:
        _set(conn, dep_id, status="building")
    dep = conn.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    try:
        if dep["provider"] == COOLIFY:
            _run_coolify(conn, app_config, dep, project, beat)
        else:
            _run_simulator(conn, app_config, dep, project, beat)
    except Cancelled:
        _log(conn, dep_id, "Deployment cancelled by user", "warn", "cancelled")
        _finish(conn, dep, project, "cancelled")
    except AuthorizationRefused as exc:
        _log(conn, dep_id, str(exc), "error", "error")
        _finish(conn, dep, project, "failed", error=str(exc))
    except CoolifyError as exc:
        _log(conn, dep_id, str(exc), "error", "error")
        _finish(conn, dep, project, "failed", error=str(exc))
    # A build that did not go live must not leave a working credential behind.
    final = conn.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    if final is not None and final["status"] in ("failed", "cancelled"):
        note = revoke_authz_deployment(conn, app_config, final, f"build {final['status']}")
        if note:
            _log(conn, dep_id, note, "info", final["status"])


@handler("expire")
def expire_deployment(conn, app_config, payload, beat):
    """TTL reached: tear the deployment down and mark it expired.

    The Coolify service is only removed when this deployment is the one it is
    currently serving. Codeskys deletes **only** services it created itself — the
    guard is that it must match the uuid Codeskys stored, the name Codeskys gave it and
    the ownership marker in its description.
    """
    dep_id = payload["deployment_id"]
    dep = conn.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    if dep is None or dep["expired_at"]:
        return
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (dep["project_id"],)).fetchone()
    if project is None:
        return
    note = "Deployment expired."
    is_live = project["current_deployment_id"] == dep["id"]
    # Credential first: even if removing the app fails below, it can no longer
    # reach protected Codeskys services.
    revoked = revoke_authz_deployment(conn, app_config, dep, "deployment expired")
    if is_live and dep["provider"] == COOLIFY and project["provider_ref"]:
        note = teardown_coolify_app(conn, app_config, project)
        still = conn.execute("SELECT provider_ref FROM projects WHERE id = ?", (project["id"],)).fetchone()
        if still and still["provider_ref"] and "Refused" not in note:
            # Coolify could not be reached / refused the delete: the app may still be
            # serving. Retry until it is gone (the 24 h limit must actually end it).
            schedule_teardown_retry(conn, project["id"], still["provider_ref"])
            note = f"{note} Removal will be retried automatically."
    elif dep["provider"] == SIMULATOR:
        note = "Simulated deployment expired; it no longer serves."
    if revoked:
        note = f"{note} {revoked}"
    conn.execute("UPDATE deployments SET status='expired', stage='expired', expired_at=?, teardown_note=? "
                 "WHERE id = ?", (utcnow(), note[:500], dep_id))
    if is_live:
        conn.execute("UPDATE projects SET current_deployment_id = NULL, updated_at = ? WHERE id = ?",
                     (utcnow(), project["id"]))
    conn.commit()
    _log(conn, dep_id, note, "warn", "expired")
    conn.execute("INSERT INTO audit_log (ts, action, target, detail, project_id) VALUES (?,?,?,?,?)",
                 (utcnow(), "deploy.expired", f"#{dep['number']}", note[:200], project["id"]))
    conn.commit()
    _notify(conn, project["owner_id"], "deploy.expired", f"{project['name']} has expired",
            f"Deploy #{dep['number']} reached its {project['ttl_hours']}-hour limit. Redeploy to bring it back.",
            f"#/projects/{project['slug']}")


def teardown_coolify_app(conn, app_config, project) -> str:
    """Stop and delete the Coolify service Codeskys created for this project. Never raises."""
    uuid = project["provider_ref"]
    if not uuid:
        return "No live application was attached to this project."
    try:
        api, _ = client_for(conn, app_config)
    except CoolifyError as exc:
        return f"Expired, but the deployment engine could not be reached to remove the application: {exc}"
    # Guard: only ever touch an application Codeskys itself created and named.
    try:
        remote = api.get_app(uuid)
    except CoolifyError as exc:
        if "404" in str(exc):
            conn.execute("UPDATE projects SET provider_ref = NULL WHERE id = ?", (project["id"],))
            conn.commit()
            return "The application was already gone."
        return f"Expired, but the application could not be inspected: {exc}"
    problem = ownership_problem(remote, project)
    if problem:
        return f"Expired. Refused to delete application {uuid}: {problem}."
    try:
        api.stop_app(uuid)
    except CoolifyError:
        pass  # a stopped or already-removed app still needs deleting
    try:
        api.delete_app(uuid)
    except CoolifyError as exc:
        return f"Expired and stopped, but deleting the application failed: {exc}"
    conn.execute("UPDATE projects SET provider_ref = NULL, coolify_app_name = '' WHERE id = ?", (project["id"],))
    conn.commit()
    return f"Expired. Application {uuid} was stopped and deleted."


# ---------------------------------------------------------------- durable retries

TEARDOWN_RETRY_ATTEMPTS = 300      # every 5 min for ~25 h per job


def schedule_teardown_retry(conn, project_id: int, app_ref: str):
    # Dedupe keys are unique forever, so the key carries the UTC day: if a job
    # exhausts its attempts, the sweep can still queue a new one the next day.
    # (Two overlapping jobs are harmless: the handler re-checks and is idempotent.)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return enqueue(conn, "coolify_teardown", {"project_id": project_id, "app_ref": app_ref},
                   dedupe_key=f"coolify-teardown:{project_id}:{app_ref}:{day}", delay=15,
                   max_attempts=TEARDOWN_RETRY_ATTEMPTS)


def teardown_pending(conn, project_id: int, app_ref: str | None = None) -> bool:
    """True while an expired project's Coolify app still exists and nothing new is
    deploying or live. Re-checked at run time so a retry never removes an app a
    newer deployment is using."""
    p = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if p is None or not p["provider_ref"] or p["current_deployment_id"] is not None:
        return False
    if app_ref is not None and p["provider_ref"] != app_ref:
        return False
    latest = conn.execute("SELECT status FROM deployments WHERE project_id = ? ORDER BY id DESC LIMIT 1",
                          (project_id,)).fetchone()
    if latest is None or latest["status"] != "expired":
        return False
    busy = conn.execute("SELECT 1 FROM deployments WHERE project_id = ? AND status IN ('queued','building')",
                        (project_id,)).fetchone()
    return busy is None


@handler("coolify_teardown")
def retry_teardown(conn, app_config, payload, beat):
    pid, ref = payload["project_id"], payload.get("app_ref")
    if not teardown_pending(conn, pid, ref):
        return                                  # already gone, or a newer deployment owns it
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    note = teardown_coolify_app(conn, app_config, project)
    latest = conn.execute("SELECT id FROM deployments WHERE project_id = ? ORDER BY id DESC LIMIT 1",
                          (pid,)).fetchone()
    if latest is not None:
        _log(conn, latest["id"], f"Retry: {note}", "warn", "expired")
    if "Refused" in note:
        return                                  # name guard: never retry into a foreign app
    if conn.execute("SELECT provider_ref FROM projects WHERE id = ?", (pid,)).fetchone()["provider_ref"]:
        raise RuntimeError(note)                # still there: back off and try again


QUIESCE_ATTEMPTS = 20           # with the queue's backoff: about 1.5 hours
QUIESCE_WINDOW = 60 * 50        # longer than a build may run on Coolify (45 min)


def schedule_quiesce(conn, project_id: int, app_ref: str, deployment_id: int):
    """Keep a cancelled build stopped. Coolify has no cancel for a service start: its
    stop marks the start cancelled, but a build already running on the server can
    still finish and start the container afterwards."""
    return enqueue(conn, "coolify_quiesce",
                   {"project_id": project_id, "app_ref": app_ref, "deployment_id": deployment_id},
                   dedupe_key=f"coolify-quiesce:{deployment_id}", delay=30, max_attempts=QUIESCE_ATTEMPTS)


@handler("coolify_quiesce")
def quiesce_cancelled_build(conn, app_config, payload, beat):
    """Stop the service again whenever it runs, until the build window is over.
    Raising means "look again later" (the queue's retry backoff)."""
    pid, ref, dep_id = payload["project_id"], payload["app_ref"], payload["deployment_id"]
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    if project is None or project["provider_ref"] != ref:
        return                                  # deleted, or the project has another service now
    if conn.execute("SELECT 1 FROM deployments WHERE project_id = ? AND id > ?", (pid, dep_id)).fetchone():
        return                                  # a newer deployment owns the service
    api, _ = client_for(conn, app_config)       # CoolifyError -> retried
    try:
        remote = api.get_app(ref)
    except CoolifyError as exc:
        if "404" in str(exc):
            return
        raise
    if ownership_problem(remote, project):
        return                                  # never touch a service Codeskys did not create
    status = str(remote.get("status") or "").lower()
    if not status.startswith(("exited", "stopped", "unknown")):
        if conn.execute("SELECT 1 FROM deployments WHERE project_id = ? AND id > ?", (pid, dep_id)).fetchone():
            return                              # a deploy started while Coolify was asked
        api.stop_app(ref)
        _log(conn, dep_id, f"The cancelled build's app was {status or 'running'}; stopped it again.",
             "warn", "cancelled")
        raise RuntimeError("stopped again; checking later")
    dep = conn.execute("SELECT finished_at FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    if dep is not None and dep["finished_at"] and _ms_since(dep["finished_at"]) < QUIESCE_WINDOW * 1000:
        raise RuntimeError("stopped; watching until the build window is over")


@handler("authz_revoke")
def retry_revoke(conn, app_config, payload, beat):
    dep_ref = payload["authz_deployment_id"]
    dep_id = payload.get("deployment_id")
    row = conn.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone() if dep_id else None
    if row is not None and (row["authz_revoked_at"] or row["authz_deployment_id"] != dep_ref):
        return
    runtime_secrets.discard(app_config, dep_ref)        # idempotent
    _revoke_remote(app_config, dep_ref, payload.get("reason") or "revocation retry")   # raises -> retry
    if row is not None:
        conn.execute("UPDATE deployments SET authz_revoked_at = ? WHERE id = ?", (utcnow(), row["id"]))
        conn.commit()
        _log(conn, row["id"], f"Authorization credential {dep_ref} revoked (retry).", "info", row["status"])


# ---------------------------------------------------------------- simulator

def _run_simulator(conn, app_config, dep, project, beat):
    """Walks the pipeline and writes logs. Executes no repository code, ever."""
    fw = FRAMEWORKS.get(project["framework"], FRAMEWORKS["static"])
    dep_id = dep["id"]
    rnd = random.Random(dep_id)
    speed = float(app_config.get("SIM_SPEED", 1.0))

    def pause(lo, hi):
        end = time.time() + rnd.uniform(lo, hi) * speed
        while time.time() < end:
            if _cancel_requested(conn, dep_id):
                raise Cancelled()
            time.sleep(min(0.08, max(0.0, end - time.time())))
        if _cancel_requested(conn, dep_id):
            raise Cancelled()
        beat()

    def stage(name):
        _set(conn, dep_id, stage=name)

    env_count = conn.execute(
        "SELECT COUNT(*) AS n FROM env_vars WHERE project_id = ? AND (',' || targets || ',') LIKE ?",
        (project["id"], f"%,{dep['target']},%")).fetchone()["n"]
    install = project["install_command"] or fw["install"]
    build = project["build_command"] or fw["build"]
    out = project["output_dir"] or fw["output"] or "."

    stage("initializing")
    _log(conn, dep_id, "SIMULATOR — this build is not real. No repository code is fetched, built or run.",
         "warn", "initializing")
    _log(conn, dep_id, "Real deployments start once an administrator connects the deployment engine.", "warn", "initializing")
    pause(0.3, 0.7)
    stage("cloning")
    _log(conn, dep_id, f"Would clone {project['repo_url']} (branch: {dep['branch']})", stage="cloning")
    pause(0.5, 1.1)
    _log(conn, dep_id, f"Commit {dep['commit_sha'][:7]} — {dep['commit_message'] or 'latest commit'}", stage="cloning")
    if project["root_dir"]:
        _log(conn, dep_id, f"Root directory: {project['root_dir']}", stage="cloning")
    _log(conn, dep_id, f"Framework preset: {fw['name']}", stage="cloning")
    _log(conn, dep_id, f"{env_count} environment variable(s) would be injected for {dep['target']}", stage="cloning")
    if install:
        stage("installing")
        _log(conn, dep_id, f"Would run \"{install}\"", stage="installing")
        pause(0.8, 1.8)
        _log(conn, dep_id, f"(simulated) added {rnd.randint(180, 1350)} packages", stage="installing")
    stage("building")
    if build:
        _log(conn, dep_id, f"Would run \"{build}\"", stage="building")
        pause(1.0, 2.2)
        if "exit 1" in build or "--fail" in build:
            _log(conn, dep_id, "(simulated) Command failed with exit code 1", "error", "building")
            _finish(conn, dep, project, "failed", error=f"Build command \"{build}\" exited with code 1.")
            return
        for _ in range(rnd.randint(2, 5)):
            _log(conn, dep_id, f"  {out}/assets/{secrets.token_hex(4)}.js   {rnd.uniform(1.2, 180):.1f} kB", stage="building")
        _log(conn, dep_id, f"(simulated) built in {rnd.uniform(2.0, 14.0):.2f}s", "success", "building")
    else:
        _log(conn, dep_id, "No build command — files would be served as-is", stage="building")
        pause(0.2, 0.5)
    stage("deploying")
    _log(conn, dep_id, f"Would publish output from \"{out}\" with "
                       f"{project['limits_memory']} memory / {project['limits_cpus']} CPU limits", stage="deploying")
    pause(0.4, 0.9)
    ttl = project["ttl_hours"] or 24
    url = f"/sites/{project['slug']}/" if dep["target"] == "production" else f"/sites/{project['slug']}/?deploy={dep['id']}"
    _log(conn, dep_id, f"Simulated deployment ready → {url} (expires in {ttl}h)", "success", "ready")
    _finish(conn, dep, project, "ready", url=url)


# ---------------------------------------------------------------- coolify

def coolify_refusal(app_config, project) -> str:
    """Why this project cannot be built on Coolify, or "" when it can.

    Only one build path exists: a Coolify service made from a compose file Codeskys
    generates (buildpolicy has why). Coolify's build packs run
    ``docker build --network host`` and are never used.
    """
    if not _col(project, "authz_project_id", ""):
        return "Link this project to your Codeskys authorization before deploying (Project → Settings)."
    if project["is_private"]:
        return ("Private repositories cannot be deployed yet (the build needs a "
                "read-only clone path). Deploy a public repository.")
    if project["framework"] not in FRAMEWORKS:
        return "Unknown framework. Choose one of the frameworks in Project → Settings."
    return ""


def owner_marker(project) -> str:
    """Description Codeskys gives every Coolify service it creates (checked before any
    change, stop, start, restart or delete, together with the name). Coolify 4.7.5
    accepts letters, digits, spaces and - _ . , ! ? ( ) ' " + = * @ / & only."""
    return f"Managed by Codeskys - project {project['id']} ({project['slug']}). Do not edit."


def ownership_problem(remote: dict, project) -> str:
    """"" when ``remote`` is the service Codeskys created for ``project``, else why not."""
    expected = project["coolify_app_name"] or project["slug"]
    if (remote.get("name") or "") != expected:
        return f"its name {remote.get('name')!r} is not the one Codeskys created ({expected!r})"
    if (remote.get("description") or "") != owner_marker(project):
        return "it does not carry the Codeskys ownership marker"
    if remote.get("service_type"):
        return f"it is a template service ({remote.get('service_type')!r})"
    return ""


def verify_coolify_owner(api, app_uuid: str, project) -> dict:
    """The service, if it is the one Codeskys created for ``project``; else CoolifyError."""
    remote = api.get_app(app_uuid)
    problem = ownership_problem(remote, project)
    if problem:
        raise CoolifyError(f"Refusing to touch app service {app_uuid}: {problem}. "
                           "An administrator must check it.")
    return remote


def verify_coolify_app(api, app_uuid: str, project, expected_compose: dict | None = None) -> dict:
    """Refuse to touch, build or start a service that is not exactly ours, in a safe state."""
    remote = api.get_app(app_uuid)
    problems = [p for p in [ownership_problem(remote, project)] if p]
    problems += buildpolicy.coolify_service_problems(remote, expected_compose)
    if problems:
        raise CoolifyError("Refusing to build: the app service is not in the expected safe state ("
                           + "; ".join(problems) + "). An administrator must check it.")
    return remote


def service_url(remote: dict) -> str:
    """The ``app`` container's first URL as Coolify stored it (without the container
    port Codeskys appends for routing), or ""."""
    for sub in remote.get("applications") or []:
        if isinstance(sub, dict) and sub.get("name") == "app":
            first = (sub.get("fqdn") or "").split(",")[0].strip()
            parts = urlsplit(first)
            return f"{parts.scheme}://{parts.hostname}{parts.path.rstrip('/')}" if parts.hostname else ""
    return ""


def _run_coolify(conn, app_config, dep, project, beat):
    api, s = client_for(conn, app_config)
    dep_id = dep["id"]
    domain, public_url = app_domains(project["slug"], s)
    remote = dep["provider_ref"]

    # Restart recovery: a deployment already handed to Coolify is resumed, not re-triggered.
    if remote:
        _log(conn, dep_id, f"Resuming deployment {remote} after a worker restart.", "warn", "building")
        return _poll_coolify(conn, api, dep, project, remote, public_url, beat, s, resumed=True)

    _set(conn, dep_id, stage="provisioning")
    refusal = coolify_refusal(app_config, project)
    if refusal:
        raise CoolifyError(refusal)
    if not domain:
        raise CoolifyError("This app has no public address configured: an administrator must set the "
                           "wildcard base domain before deployments can go live.")
    return _run_coolify_compose(conn, app_config, api, s, dep, project, domain, beat, public_url)


def _run_coolify_compose(conn, app_config, api, s, dep, project, domain, beat, public_url=None):
    """The student's app as one Coolify service, from a compose file Codeskys generates.

    Checks that need no Coolify call come first, then authz, then the service is
    created (or its compose file replaced), verified and started. A service this
    deployment created is deleted again when a step before the start fails. The
    student repository is never modified.
    """
    import base64
    from . import composegen
    dep_id = dep["id"]
    if project["framework"] == "docker":
        # The repository's own Dockerfile (framework projects get one generated by Codeskys,
        # which composegen.assert_safe_service checks line by line).
        root = (project["root_dir"] or "").strip("/")
        try:
            buildpolicy.check_dockerfile(project["repo_url"], dep["commit_sha"],
                                         f"{root}/Dockerfile" if root else "Dockerfile")
        except buildpolicy.PolicyError as exc:
            raise CoolifyError(str(exc)) from None
        _log(conn, dep_id, "Dockerfile checked: no host networking, insecure mode or devices requested.",
             stage="provisioning")
    else:
        from . import dockerfiles
        try:
            dockerfiles.settings(project)                        # fail before Coolify is asked anything
        except dockerfiles.DockerfileError as exc:
            raise CoolifyError(str(exc)) from None
        _log(conn, dep_id, f"Building with the Codeskys {FRAMEWORKS[project['framework']]['name']} "
                           "Dockerfile (isolated build, unprivileged container).", stage="provisioning")
    authorize_deployment(conn, app_config, dep, project, with_credential=False)   # raises AuthorizationRefused
    dep = conn.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project["id"],)).fetchone()
    keys = [r["key"] for r in conn.execute(
        "SELECT key FROM env_vars WHERE project_id = ? AND (',' || targets || ',') LIKE ?",
        (project["id"], f"%,{dep['target']},%"))]
    try:
        doc = composegen.for_service(project, dep, env_keys=keys)
    except composegen.ComposeError as exc:
        raise CoolifyError(f"Could not prepare the deployment: {exc}.") from None
    skipped = sorted({k for k in keys if composegen.coolify_reserved(k)})
    if skipped:
        _log(conn, dep_id, "Not passed to the app (names reserved by Codeskys): " + ", ".join(skipped),
             "warn", "provisioning")
    raw = base64.b64encode(composegen.render(doc).encode("utf-8")).decode("ascii")
    _log(conn, dep_id, "Prepared the app as an isolated service (its own network, shared only with "
                       "the proxy; no route to Codeskys internals).", stage="provisioning")
    # The port tells Traefik which container port to use; the Host rule has no port.
    fields = {"description": owner_marker(project), "docker_compose_raw": raw, "connect_to_docker_network": False,
              "urls": [{"name": "app", "url": f"{domain}:{composegen.service_port(doc)}"}]}
    app_uuid, created = project["provider_ref"], False
    if app_uuid:
        verify_coolify_owner(api, app_uuid, project)             # never change a service that is not ours
        api.update_service(app_uuid, fields)
    else:
        body = {"project_uuid": s["project_uuid"], "server_uuid": s["server_uuid"],
                "environment_name": s["environment_name"], "name": project["slug"], "instant_deploy": False,
                **fields}
        if s.get("destination_uuid"):
            body["destination_uuid"] = s["destination_uuid"]
        app_uuid, created = api.create_service(body), True
        conn.execute("UPDATE projects SET provider_ref = ?, coolify_app_name = ?, provider = 'coolify' WHERE id = ?",
                     (app_uuid, project["slug"], project["id"]))
        conn.commit()
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (project["id"],)).fetchone()
        _log(conn, dep_id, f"App service {app_uuid} created.", stage="provisioning")
    try:
        box = Secrets(app_config["ENCRYPTION_KEY"])
        values = []
        for e in conn.execute("SELECT key, value, value_cipher, is_secret, targets FROM env_vars "
                              "WHERE project_id = ?", (project["id"],)).fetchall():
            if dep["target"] not in e["targets"].split(",") or composegen.coolify_reserved(e["key"]):
                continue
            v = box.decrypt(e["value_cipher"]) if e["is_secret"] and e["value_cipher"] else (e["value"] or "")
            # Literal: Coolify must not expand ${...} in a student's value.
            values.append({"key": e["key"], "value": v, "is_literal": True})
        api.bulk_envs(app_uuid, values)
        was_running = _app_running(str(verify_coolify_app(api, app_uuid, project, doc).get("status") or ""))
        beat()
        if _cancel_requested(conn, dep_id):
            raise Cancelled()
    except (CoolifyError, Cancelled):
        if created:
            _remove_unstarted_service(conn, api, dep_id, project)
        raise
    _set(conn, dep_id, stage="building")
    api.run_app(app_uuid)
    _set(conn, dep_id, provider_ref=app_uuid)
    _log(conn, dep_id, "Building and starting the app. Build output is not reported for this kind of "
                       "deployment; the app's own output is shown if it fails to start.", stage="building")
    return _poll_coolify(conn, api, dep, project, app_uuid, public_url or domain, beat, s,
                         previous_running=was_running)


def _remove_unstarted_service(conn, api, dep_id, project):
    """Rollback: delete the service this deployment created before anything was started.
    If that fails the service stays attached to the project and the next deploy reuses it."""
    uuid = project["provider_ref"]
    try:
        verify_coolify_owner(api, uuid, project)
        api.delete_app(uuid)
    except CoolifyError as exc:
        _log(conn, dep_id, f"The new app service {uuid} could not be removed ({exc}); the next deploy "
                           "reuses it.", "warn", "provisioning")
        return
    conn.execute("UPDATE projects SET provider_ref = NULL, coolify_app_name = '' WHERE id = ?", (project["id"],))
    conn.commit()
    _log(conn, dep_id, f"The new app service {uuid} was removed again.", "warn", "provisioning")


POLL_SECONDS = 3            # between Coolify status reads (tests shorten it)
POLL_ERROR_LIMIT = 20       # consecutive failed reads (~1 minute) before giving up
START_GRACE = 90            # seconds Coolify's queue may take to begin a start
BUILD_TIMEOUT = 60 * 45


def _poll_coolify(conn, api, dep, project, remote, domain, beat, settings=None, resumed=False,
                  previous_running=False):
    """Wait for Coolify's build/start (status "starting:…") to end, then check readiness.

    Coolify returns no deployment id for a service, so the only progress signal is the
    status. When the previous version was running before the start, "running" means
    nothing until Coolify has shown "starting" (the redeploy may still be queued).
    """
    dep_id = dep["id"]
    errors, seen_starting = 0, False
    begun = time.time()
    while time.time() < begun + BUILD_TIMEOUT:
        if _cancel_requested(conn, dep_id):
            _cancel_coolify_build(conn, api, dep_id, project, beat)
            raise Cancelled()
        try:
            status = str(api.get_app(remote).get("status") or "").lower()
        except CoolifyError as exc:
            # A short Coolify outage must not fail a build that is still running there.
            errors += 1
            if errors >= POLL_ERROR_LIMIT:
                raise CoolifyError(f"Lost contact with the deployment engine while building: {exc}") from None
            if errors == 1:
                _log(conn, dep_id, f"The deployment engine did not answer ({exc}); retrying.", "warn", "building")
            beat()
            time.sleep(POLL_SECONDS)
            continue
        errors = 0
        if status.startswith("starting"):
            seen_starting = True
        elif seen_starting or resumed:
            break
        elif previous_running:
            if time.time() - begun >= START_GRACE:
                _finish(conn, dep, project, "failed",
                        error=f"The new version did not start within {START_GRACE} seconds; the "
                              "previous version is still running. Try again in a few minutes.")
                return
        elif _app_running(status) or time.time() - begun >= START_GRACE:
            break
        beat()
        time.sleep(POLL_SECONDS)
    else:
        _stop_unready_app(conn, api, dep_id, project)
        _finish(conn, dep, project, "failed", error="Timed out after 45 minutes waiting for the build.")
        return
    # The build finishing is not the app working: verify before "ready",
    # so the 24-hour window only starts once the URL actually serves the app.
    problem = verify_app_ready(conn, api, dep, project, domain, beat, settings or {})
    if problem:
        _stop_unready_app(conn, api, dep_id, project)
        _finish(conn, dep, project, "failed", error=problem)
        return
    _log(conn, dep_id, f"Deployment ready → {domain}", "success", "ready")
    _finish(conn, dep, project, "ready", url=domain)


def _cancel_coolify_build(conn, api, dep_id, project, beat):
    """Stop the service (Coolify has no cancel for a service start), retrying a few
    times, then keep it stopped with a durable job."""
    for attempt in range(3):
        try:
            api.stop_app(project["provider_ref"])
            break
        except CoolifyError as exc:
            if attempt == 2:
                _log(conn, dep_id, f"The deployment engine could not stop the build: {exc}", "warn", "cancelled")
            else:
                beat()
                time.sleep(POLL_SECONDS)
    conn.execute("UPDATE projects SET app_state = 'stopped' WHERE id = ?", (project["id"],))
    conn.commit()
    schedule_quiesce(conn, project["id"], project["provider_ref"], dep_id)


# ---------------------------------------------------------------- readiness

# Traefik's answer for a hostname it has no route for. A student app answering
# 404 at "/" is fine (it is running); this exact body means nothing is behind the URL.
_NO_ROUTE = "404 page not found"


def probe_app(url: str, probe_base: str = "", timeout: float = 5.0) -> tuple:
    """(ok, detail) for one HTTP request to the app.

    With ``probe_base`` (e.g. ``http://coolify-proxy:80``) the request goes there
    with the app's Host header, inside the server; otherwise to ``url`` itself.
    Redirects are not followed: a 3xx means the app answered.
    """
    parts = urlsplit(url)
    target, headers = url, {"User-Agent": "Codeskys-readiness/1"}
    if probe_base:
        target = probe_base.rstrip("/") + (parts.path or "/")
        headers["Host"] = parts.hostname or ""
    try:
        r = requests.get(target, headers=headers, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        return False, f"no answer ({exc.__class__.__name__})"
    if r.status_code == 404 and (r.text or "").strip() == _NO_ROUTE:
        return False, "the proxy has no route to the app yet"
    if r.status_code >= 500:
        return False, f"HTTP {r.status_code}"
    return True, f"HTTP {r.status_code}"


def _app_running(status: str) -> bool:
    status = (status or "").lower()
    return status.startswith("running") and "unhealthy" not in status


def verify_app_ready(conn, api, dep, project, url, beat, settings) -> str:
    """"" once the application is running and its URL answers, else why not.

    Two consecutive passing checks are required, so a container that starts and
    then crash-loops is not reported as live.
    """
    dep_id = dep["id"]
    if not url:
        return ("The build finished, but no public address is configured for this app. "
                "An administrator must set the wildcard base domain.")
    _set(conn, dep_id, stage="deploying")
    _log(conn, dep_id, f"Build finished. Checking that the app is running and answers at {url} …",
         stage="deploying")
    timeout = int(settings.get("ready_timeout") or 180)
    deadline = time.time() + timeout
    passes, last = 0, "the application did not report as running"
    while True:
        if _cancel_requested(conn, dep_id):
            _stop_unready_app(conn, api, dep_id, project)
            raise Cancelled()
        try:
            state = str(api.get_app(project["provider_ref"]).get("status") or "")
        except CoolifyError as exc:
            state, last = "", f"could not read the application status ({exc})"
        if _app_running(state):
            ok, detail = probe_app(url, settings.get("probe_url", ""))
            if ok:
                passes += 1
                if passes >= 2:
                    _log(conn, dep_id, f"Health check passed ({detail}).", "success", "deploying")
                    return ""
            else:
                passes, last = 0, f"the URL check failed: {detail}"
        else:
            passes = 0
            if state:
                last = f"the application is {state}"
        if time.time() >= deadline:
            break
        beat()
        time.sleep(POLL_SECONDS)
    _copy_app_logs(conn, api, dep_id, project)
    return (f"The build finished, but the app did not become available within {timeout} seconds "
            f"({last}). Check that it listens on the expected port; the log above has its output.")


def _copy_app_logs(conn, api, dep_id, project, lines=40):
    """The app's own output, so a crash at start-up is visible in the build log."""
    try:
        data = api.app_logs(project["provider_ref"], lines=lines)
    except CoolifyError:
        return
    text = data.get("logs") if isinstance(data, dict) else data
    if isinstance(text, str) and text.strip():
        _log(conn, dep_id, "Application output (last lines):", "warn", "deploying")
        for line in text.strip().splitlines()[-lines:]:
            _log(conn, dep_id, line, "error", "deploying")


def _stop_unready_app(conn, api, dep_id, project):
    """A build that did not go live must not stay running (crash loops, half-started apps)."""
    try:
        api.stop_app(project["provider_ref"])
        _log(conn, dep_id, "The application was stopped.", "warn", "deploying")
    except CoolifyError as exc:
        _log(conn, dep_id, f"The deployment engine could not stop the application: {exc}", "warn", "deploying")
    if project["current_deployment_id"] and project["current_deployment_id"] != dep_id:
        # Coolify replaced the previous containers with this build, so that version is gone too.
        _log(conn, dep_id, "The previously published version is no longer running. "
                           "Fix the problem and deploy again.", "warn", "deploying")
    conn.execute("UPDATE projects SET app_state = 'stopped' WHERE id = ?", (project["id"],))
    conn.commit()
