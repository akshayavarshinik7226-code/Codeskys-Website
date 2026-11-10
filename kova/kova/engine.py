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
    Creates and drives a real application on a Coolify server over its v1 API.

Cancellation crosses process boundaries through the database: the API sets
``stage='cancelling'`` and the worker notices on its next checkpoint.
"""
import json
import random
import secrets
import time
from datetime import datetime, timedelta, timezone

import requests

from . import authz_client, runtime_secrets
from .db import get_setting, utcnow
from .crypto import Secrets
from .gitinfo import FRAMEWORKS, latest_commit
from .jobs import enqueue, handler

SIMULATOR = "simulator"
COOLIFY = "coolify"


class CoolifyError(RuntimeError):
    """A Coolify call failed. Always surfaced to the user verbatim — never swallowed."""


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
    }


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
            raise CoolifyError(f"Could not reach Coolify at {self.base}: {exc.__class__.__name__}") from exc
        if r.status_code == 401:
            raise CoolifyError("Coolify rejected the API token (401). Check the token and its permissions.")
        if r.status_code == 403:
            raise CoolifyError("Coolify denied this action (403). The token is missing a required permission.")
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message") or body.get("error") or json.dumps(body)[:300]
            except ValueError:
                msg = r.text[:300]
            raise CoolifyError(f"Coolify returned {r.status_code}: {msg}")
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

    # -- applications
    def create_public_app(self, body: dict) -> str:
        return self._req("POST", "/applications/public", json=body)["uuid"]

    def create_private_app(self, body: dict) -> str:
        return self._req("POST", "/applications/private-deploy-key", json=body)["uuid"]

    def create_compose_app(self, body: dict) -> str:
        """Docker Compose application from raw compose (base64, per the v4 API).
        UNVERIFIED against a live Coolify: check the field names after install."""
        return self._req("POST", "/applications/dockercompose", json=body)["uuid"]

    def create_github_app_app(self, body: dict) -> str:
        """Private repository through a Coolify "GitHub App" source (read-only App)."""
        return self._req("POST", "/applications/private-github-app", json=body)["uuid"]

    def get_app(self, uuid: str):
        return self._req("GET", f"/applications/{uuid}")

    def update_app(self, uuid: str, body: dict):
        return self._req("PATCH", f"/applications/{uuid}", json=body)

    def bulk_envs(self, uuid: str, envs: list):
        if envs:
            return self._req("PATCH", f"/applications/{uuid}/envs/bulk", json={"data": envs})

    def app_logs(self, uuid: str, lines: int = 200):
        return self._req("GET", f"/applications/{uuid}/logs", params={"lines": lines})

    def _lifecycle(self, uuid: str, action: str, params=None):
        """start / stop / restart. Coolify v4 serves these as GET; newer builds also
        accept POST. Try POST first and fall back on 405, like ``deploy``."""
        try:
            return self._req("POST", f"/applications/{uuid}/{action}", params=params)
        except CoolifyError as exc:
            if "405" not in str(exc):
                raise
            return self._req("GET", f"/applications/{uuid}/{action}", params=params)

    def stop_app(self, uuid: str):
        return self._lifecycle(uuid, "stop", {"docker_cleanup": "true"})

    def start_app(self, uuid: str):
        return self._lifecycle(uuid, "start")

    def restart_app(self, uuid: str):
        return self._lifecycle(uuid, "restart")

    def delete_app(self, uuid: str):
        return self._req("DELETE", f"/applications/{uuid}", params={
            "delete_configurations": "true", "delete_volumes": "true",
            "docker_cleanup": "true", "delete_connected_networks": "false"})

    # -- deployments
    def deploy(self, uuid: str, force=False) -> str:
        """Trigger a build. Current Coolify exposes POST /deploy; older builds used GET."""
        params = {"uuid": uuid, "force": "true" if force else "false"}
        try:
            data = self._req("POST", "/deploy", params=params)
        except CoolifyError as exc:
            if "405" not in str(exc):
                raise
            data = self._req("GET", "/deploy", params=params)
        deployments = data.get("deployments") or []
        if not deployments:
            raise CoolifyError(data.get("message") or "Coolify accepted the request but started no deployment.")
        return deployments[0]["deployment_uuid"]

    def deployment(self, dep_uuid: str):
        return self._req("GET", f"/deployments/{dep_uuid}")

    def cancel_deployment(self, dep_uuid: str):
        return self._req("POST", f"/deployments/{dep_uuid}/cancel")


def client_for(conn, app_config):
    s = coolify_settings(conn, app_config)
    if not (s["url"] and s["token"]):
        raise CoolifyError("Coolify is not configured. Add the instance URL and API token in Integrations.")
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


def authorize_deployment(conn, app_config, dep, project):
    """Verify the repository with authz and, when a runtime-secret sink exists,
    obtain a deployment-bound credential for this REAL build.

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


def revoke_authz_deployment(conn, app_config, dep, reason: str) -> str:
    """Revoke a deployment's credential. Idempotent; never raises."""
    dep_ref = _col(dep, "authz_deployment_id", "")
    if not dep_ref or _col(dep, "authz_revoked_at"):
        return ""
    client = authz_client.client_from_config(app_config)
    if client is None:
        return f"Authorization credential {dep_ref} could not be revoked: authz is not configured."
    try:
        client.revoke_deployment(dep_ref, reason)
    except authz_client.AuthzError as exc:
        if exc.status != 404:
            return f"Authorization credential {dep_ref} could not be revoked ({exc.describe()}); retry later."
    runtime_secrets.discard(app_config, dep_ref)
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

    The Coolify application is only removed when this deployment is the one it is
    currently serving. Codeskys deletes **only** applications it created itself — the
    guard is that it must match both the uuid Codeskys stored and the name Codeskys gave it.
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
    if is_live and dep["provider"] == COOLIFY and project["provider_ref"]:
        note = teardown_coolify_app(conn, app_config, project)
    elif dep["provider"] == SIMULATOR:
        note = "Simulated deployment expired; it no longer serves."
    revoked = revoke_authz_deployment(conn, app_config, dep, "deployment expired")
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
    """Stop and delete the Coolify application Codeskys created for this project."""
    uuid = project["provider_ref"]
    if not uuid:
        return "No Coolify application was attached to this project."
    try:
        api, _ = client_for(conn, app_config)
    except CoolifyError as exc:
        return f"Expired, but Coolify could not be reached to remove the application: {exc}"
    # Guard: only ever touch an application Codeskys itself created and named.
    expected = project["coolify_app_name"] or project["slug"]
    try:
        remote = api.get_app(uuid)
    except CoolifyError as exc:
        if "404" in str(exc):
            conn.execute("UPDATE projects SET provider_ref = NULL WHERE id = ?", (project["id"],))
            conn.commit()
            return "Coolify application was already gone."
        return f"Expired, but the Coolify application could not be inspected: {exc}"
    remote_name = (remote.get("name") or "")
    if remote_name and expected and remote_name != expected:
        return (f"Expired. Refused to delete Coolify application {uuid}: its name "
                f"'{remote_name}' does not match the one Codeskys created ('{expected}').")
    try:
        api.stop_app(uuid)
    except CoolifyError:
        pass  # a stopped or already-removed app still needs deleting
    try:
        api.delete_app(uuid)
    except CoolifyError as exc:
        return f"Expired and stopped, but deleting the Coolify application failed: {exc}"
    conn.execute("UPDATE projects SET provider_ref = NULL, coolify_app_name = '' WHERE id = ?", (project["id"],))
    conn.commit()
    return f"Expired. Coolify application {uuid} was stopped and deleted."


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
    _log(conn, dep_id, "Connect a Coolify server in Integrations to produce real deployments.", "warn", "initializing")
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

def _build_pack(project):
    if project["framework"] == "docker":
        return "dockerfile"
    if project["framework"] == "static" and not (project["build_command"] or FRAMEWORKS.get(project["framework"], {}).get("build")):
        return "static"
    return "nixpacks"


def _ssh_url(repo_url: str) -> str:
    """Coolify clones private repos over SSH with the deploy key."""
    u = repo_url.replace("https://", "").replace("http://", "").rstrip("/").removesuffix(".git")
    host, _, path = u.partition("/")
    return f"git@{host}:{path}.git"


def _app_body(project, dep, settings, domain):
    fw = FRAMEWORKS.get(project["framework"], FRAMEWORKS["static"])
    publish = project["output_dir"] or fw["output"]
    body = {
        "git_repository": _ssh_url(project["repo_url"]) if project["is_private"] else project["repo_url"],
        "git_branch": dep["branch"],
        "build_pack": _build_pack(project),
        "ports_exposes": str(project["port"]),
        "install_command": project["install_command"] or fw["install"],
        "build_command": project["build_command"] or fw["build"],
        "start_command": project["start_command"] or fw["start"],
        "base_directory": "/" + project["root_dir"].strip("/") if project["root_dir"] else "/",
        "publish_directory": "/" + publish.strip("/") if publish else None,
        "is_static": bool(fw["static"]),
        "is_auto_deploy_enabled": False,   # Codeskys drives deploys; Coolify must not also trigger them
        "is_force_https_enabled": bool(settings.get("force_https", True)),
        # Resource limits keep one student's build from starving the server.
        "limits_memory": project["limits_memory"] or "512M",
        "limits_cpus": project["limits_cpus"] or "0.5",
    }
    if domain:
        body["domains"] = domain
    return {k: v for k, v in body.items() if v not in (None, "")}


def _run_coolify(conn, app_config, dep, project, beat):
    api, s = client_for(conn, app_config)
    dep_id = dep["id"]
    scheme = "https" if s.get("force_https", True) else "http"
    domain = f"{scheme}://{project['slug']}.{s['base_domain']}" if s["base_domain"] else None
    remote = dep["provider_ref"]

    # Restart recovery: a deployment already handed to Coolify is resumed, not re-triggered.
    if remote:
        _log(conn, dep_id, f"Resuming Coolify deployment {remote} after a worker restart.", "warn", "building")
        return _poll_coolify(conn, api, dep, project, remote, domain, beat)

    app_uuid = project["provider_ref"]
    _set(conn, dep_id, stage="provisioning")
    if runtime_secrets.enabled(app_config) and _col(project, "authz_project_id", ""):
        return _run_coolify_compose(conn, app_config, api, s, dep, project, domain, beat)
    body = _app_body(project, dep, s, domain)
    if not app_uuid:
        create = {**body, "project_uuid": s["project_uuid"], "server_uuid": s["server_uuid"],
                  "environment_name": s["environment_name"], "name": project["slug"],
                  "description": (project["description"] or "")[:200], "instant_deploy": False}
        if s.get("destination_uuid"):
            create["destination_uuid"] = s["destination_uuid"]
        if project["is_private"] and s.get("github_app_uuid"):
            # Read-only GitHub App: Coolify mints its own short-lived installation
            # token per build. Nothing is added to the student's repository.
            create["github_app_uuid"] = s["github_app_uuid"]
            create["git_repository"] = project["repo_full_name"] or create["git_repository"]
            _log(conn, dep_id, "Creating private application on Coolify through the read-only GitHub App.",
                 stage="provisioning")
            app_uuid = api.create_github_app_app(create)
        elif project["is_private"]:
            if not s["private_key_uuid"]:
                raise CoolifyError(
                    "This repository is private. Configure the Coolify GitHub App source (Integrations → "
                    "GitHub App UUID), or a Coolify deploy key, then redeploy.")
            create["private_key_uuid"] = s["private_key_uuid"]
            _log(conn, dep_id, "Creating private application on Coolify using the configured deploy key.",
                 stage="provisioning")
            app_uuid = api.create_private_app(create)
        else:
            _log(conn, dep_id, f"Creating application on Coolify ({s['url']})", stage="provisioning")
            app_uuid = api.create_public_app(create)
        conn.execute("UPDATE projects SET provider_ref = ?, coolify_app_name = ?, provider = 'coolify' WHERE id = ?",
                     (app_uuid, project["slug"], project["id"]))
        conn.commit()
        _log(conn, dep_id, f"Application created: {app_uuid}", "success", "provisioning")
    else:
        _log(conn, dep_id, f"Syncing build settings to Coolify application {app_uuid}", stage="provisioning")
        api.update_app(app_uuid, body)
    beat()

    envs = conn.execute("SELECT key, value, value_cipher, is_secret, targets FROM env_vars WHERE project_id = ?",
                        (project["id"],)).fetchall()
    box = Secrets(app_config["ENCRYPTION_KEY"])
    payload = []
    for e in envs:
        if dep["target"] not in e["targets"].split(","):
            continue
        if e["key"] in RUNTIME_ENV:
            continue  # reserved: only Codeskys authorization may set these
        value = e["value"] or ""
        if e["is_secret"] and e["value_cipher"]:
            value = box.decrypt(e["value_cipher"])
        payload.append({"key": e["key"], "value": value, "is_preview": dep["target"] == "preview"})
    if payload:
        api.bulk_envs(app_uuid, payload)
        _log(conn, dep_id, f"Synced {len(payload)} environment variable(s)", stage="provisioning")

    # Deployment-bound credential, last, so a user variable can never shadow it.
    runtime_env = authorize_deployment(conn, app_config, dep, project)
    if runtime_env:
        api.bulk_envs(app_uuid, runtime_env)

    if _cancel_requested(conn, dep_id):
        raise Cancelled()
    _set(conn, dep_id, stage="building")
    remote = api.deploy(app_uuid, force=dep["trigger"] == "redeploy-clear-cache")
    _set(conn, dep_id, provider_ref=remote)
    _log(conn, dep_id, f"Coolify deployment started: {remote}", stage="building")
    return _poll_coolify(conn, api, dep, project, remote, domain, beat)


def _run_coolify_compose(conn, app_config, api, s, dep, project, domain, beat):
    """App + runtime-proxy sidecar, from a compose file Codeskys generates.

    Order matters: the deployment credential is obtained (and written as the
    sidecar's secret file) BEFORE the application exists, because the compose
    file names that deployment. The student repository is never modified.
    """
    import base64
    from . import composegen
    dep_id = dep["id"]
    if project["is_private"]:
        raise CoolifyError("Protected Codeskys services are not available yet for private repositories "
                           "(the sidecar build needs a read-only clone path). Deploy a public repository, "
                           "or ask an administrator to unlink authorization for this project.")
    authorize_deployment(conn, app_config, dep, project)          # raises AuthorizationRefused
    dep = conn.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project["id"],)).fetchone()
    host_dir = app_config.get("RUNTIME_SECRET_HOST_DIR") or app_config["RUNTIME_SECRET_DIR"]
    secret_host_file = f"{host_dir.rstrip('/')}/{dep['authz_deployment_id']}/runtime_secret"
    keys = [r["key"] for r in conn.execute(
        "SELECT key FROM env_vars WHERE project_id = ? AND (',' || targets || ',') LIKE ?",
        (project["id"], f"%,{dep['target']},%"))]
    try:
        doc = composegen.for_project(project, dep, app_config=app_config,
                                     secret_host_file=secret_host_file, env_keys=keys)
    except composegen.ComposeError as exc:
        raise CoolifyError(f"Could not prepare the deployment: {exc}.") from None
    raw = base64.b64encode(composegen.render(doc).encode("utf-8")).decode("ascii")
    _log(conn, dep_id, "Prepared app + Codeskys runtime sidecar (isolated private network; the app has "
                       "no route to Codeskys internals).", stage="provisioning")
    body = {"project_uuid": s["project_uuid"], "server_uuid": s["server_uuid"],
            "environment_name": s["environment_name"], "name": project["slug"],
            "docker_compose_raw": raw, "instant_deploy": False}
    if s.get("destination_uuid"):
        body["destination_uuid"] = s["destination_uuid"]
    app_uuid = project["provider_ref"]
    if app_uuid:
        api.update_app(app_uuid, {"docker_compose_raw": raw})
    else:
        app_uuid = api.create_compose_app(body)
        conn.execute("UPDATE projects SET provider_ref = ?, coolify_app_name = ?, provider = 'coolify' WHERE id = ?",
                     (app_uuid, project["slug"], project["id"]))
        conn.commit()
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (project["id"],)).fetchone()
    box = Secrets(app_config["ENCRYPTION_KEY"])
    values = []
    for e in conn.execute("SELECT key, value, value_cipher, is_secret, targets FROM env_vars WHERE project_id = ?",
                          (project["id"],)).fetchall():
        if dep["target"] not in e["targets"].split(",") or e["key"] in composegen.RESERVED:
            continue
        v = box.decrypt(e["value_cipher"]) if e["is_secret"] and e["value_cipher"] else (e["value"] or "")
        values.append({"key": e["key"], "value": v, "is_preview": dep["target"] == "preview"})
    if values:
        api.bulk_envs(app_uuid, values)
    beat()
    if _cancel_requested(conn, dep_id):
        raise Cancelled()
    _set(conn, dep_id, stage="building")
    remote = api.deploy(app_uuid, force=dep["trigger"] == "redeploy-clear-cache")
    _set(conn, dep_id, provider_ref=remote)
    _log(conn, dep_id, f"Coolify deployment started: {remote}", stage="building")
    return _poll_coolify(conn, api, dep, project, remote, domain, beat)


def _poll_coolify(conn, api, dep, project, remote, domain, beat):
    dep_id = dep["id"]
    seen = 0
    deadline = time.time() + 60 * 45
    while time.time() < deadline:
        if _cancel_requested(conn, dep_id):
            try:
                api.cancel_deployment(remote)
            except CoolifyError as exc:
                _log(conn, dep_id, f"Coolify could not cancel the build: {exc}", "warn", "cancelled")
            raise Cancelled()
        data = api.deployment(remote)
        seen = _copy_logs(conn, dep_id, data, seen)
        status = (data.get("status") or "").lower()
        if status in ("finished", "success"):
            url = domain
            try:
                fqdn = (api.get_app(project["provider_ref"]).get("fqdn") or "").split(",")[0]
                url = fqdn or domain
            except CoolifyError:
                pass
            _log(conn, dep_id, f"Deployment ready → {url or 'no domain configured'}", "success", "ready")
            _finish(conn, dep, project, "ready", url=url)
            return
        if status in ("failed", "error"):
            _finish(conn, dep, project, "failed",
                    error="Coolify reported the build as failed. The build log above has the detail.")
            return
        if status.startswith("cancel"):
            _finish(conn, dep, project, "cancelled")
            return
        beat()
        time.sleep(3)
    _finish(conn, dep, project, "failed", error="Timed out after 45 minutes waiting for Coolify.")


def _copy_logs(conn, dep_id, data, seen):
    raw = data.get("logs") or "[]"
    try:
        entries = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        entries = []
    if not isinstance(entries, list):
        return seen
    visible = [e for e in entries if isinstance(e, dict) and not e.get("hidden")]
    for entry in visible[seen:]:
        line = (entry.get("output") or "").rstrip()
        if line:
            _log(conn, dep_id, line, "error" if entry.get("type") == "stderr" else "info", "building")
    return len(visible)
