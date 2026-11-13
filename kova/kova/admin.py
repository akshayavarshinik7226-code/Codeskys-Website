"""Administrator APIs: members and the deployment-provider (Coolify) connection."""
import re
import secrets

from flask import Blueprint, current_app, jsonify, request

from . import authz_client, engine
from .auth import admin_required, current_user
from .db import audit, get_db, get_setting, set_setting, utcnow
from .security import hash_password
from .users import MIN_PASSWORD, ValidationError, create_user

bp = Blueprint("admin", __name__, url_prefix="/api/admin")


def _member(r, db):
    n = db.execute("SELECT COUNT(*) AS n FROM projects WHERE owner_id = ?", (r["id"],)).fetchone()["n"]
    return {"id": r["id"], "email": r["email"], "name": r["name"], "role": r["role"], "plan": r["plan"],
            "is_active": bool(r["is_active"]), "created_at": r["created_at"], "last_login_at": r["last_login_at"],
            "projects": n, "locked": bool(r["locked_until"])}


@bp.get("/users")
@admin_required
def users():
    db = get_db()
    rows = db.execute("SELECT * FROM users ORDER BY id").fetchall()
    return jsonify(users=[_member(r, db) for r in rows])


@bp.post("/users")
@admin_required
def add_user():
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")
    generated = None
    if not password:
        generated = password = secrets.token_urlsafe(12)
    try:
        uid = create_user(get_db(), data.get("email", ""), data.get("name", ""), password, data.get("role", "student"))
    except ValidationError as exc:
        return jsonify(error=str(exc)), 400
    plan = data.get("plan") if data.get("plan") in ("hobby", "pro", "team") else "hobby"
    db = get_db()
    db.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, uid))
    db.commit()
    audit("user.created", actor_id=current_user()["id"], target=str(data.get("email", "")).lower())
    return jsonify(user=_member(db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone(), db),
                   temporary_password=generated), 201


@bp.patch("/users/<int:uid>")
@admin_required
def edit_user(uid):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None:
        return jsonify(error="User not found."), 404
    me = current_user()
    updates = {}
    if "is_active" in data:
        if uid == me["id"] and not data["is_active"]:
            return jsonify(error="You can't deactivate your own account."), 409
        updates["is_active"] = 1 if data["is_active"] else 0
    if "role" in data:
        if data["role"] not in ("admin", "student"):
            return jsonify(error="Role must be admin or student."), 400
        if uid == me["id"] and data["role"] != "admin":
            return jsonify(error="You can't remove your own admin role."), 409
        updates["role"] = data["role"]
    if "plan" in data:
        if data["plan"] not in ("hobby", "pro", "team"):
            return jsonify(error="Unknown plan."), 400
        updates["plan"] = data["plan"]
    if data.get("unlock"):
        updates["locked_until"] = None
        updates["failed_logins"] = 0
    temp = None
    if data.get("reset_password"):
        temp = secrets.token_urlsafe(12)
        updates["password_hash"] = hash_password(temp)
    if not updates:
        return jsonify(error="Nothing to update."), 400
    cols = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE users SET {cols} WHERE id = ?", (*updates.values(), uid))
    db.commit()
    audit("user.updated", actor_id=me["id"], target=row["email"],
          detail=", ".join(k for k in updates if k != "password_hash") + (" password_reset" if temp else ""))
    return jsonify(user=_member(db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone(), db),
                   temporary_password=temp)


# ---------------------------------------------------------------- integrations

def _integration_view():
    db = get_db()
    s = engine.coolify_settings(db, current_app.config)
    return {
        "provider": get_setting(db, "deploy_provider", "simulator"),
        "active_provider": engine.active_provider(db, current_app.config),
        "coolify": {
            "url": s["url"], "token_set": bool(s["token"]),
            "token_from_env": bool(current_app.config.get("COOLIFY_TOKEN")),
            "token_hint": ("…" + s["token"][-4:]) if s["token"] else None,
            "project_uuid": s["project_uuid"], "server_uuid": s["server_uuid"],
            "environment_name": s["environment_name"], "base_domain": s["base_domain"],
            "private_key_uuid": s["private_key_uuid"],
            "github_app_uuid": s["github_app_uuid"], "force_https": s["force_https"],
            "destination_uuid": s["destination_uuid"],
            "url_from_env": bool(current_app.config.get("COOLIFY_URL")),
            "ready": engine.coolify_ready(s),
            "last_check": get_setting(db, "coolify_last_check"), "last_version": get_setting(db, "coolify_version"),
        },
    }


# ------------------------------------------------------------------ Codeskys authorization
# Contract: forgeops/authz/WEBSITE_INTEGRATION.md. The website stores only the
# authz project id and the GitHub-verified repository id; never a credential.

AUTHZ_PROJECT_RE = re.compile(r"^prj_[a-f0-9]{8,28}$")


def _authz_error(exc):
    status = exc.status if exc.status in (403, 404, 409, 422) else 502
    return jsonify(error=exc.user_message(), code=exc.code or None, reference=exc.reference or None), status


def _project_by_slug(db, slug):
    return db.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()


def _int_or_none(value):
    if value in (None, ""):
        return None
    v = int(value)
    if v < 1:
        raise ValueError
    return v


def bind_authz_project(db, p, authz_pid, installation_id, reg, actor_id, how):
    """Store the link + GitHub-verified repository id. Revokes credentials issued
    under a previous, different link."""
    if p["authz_project_id"] and p["authz_project_id"] != authz_pid:
        engine.revoke_project_credentials(db, current_app.config, p["id"], "authorization relinked")
    db.execute("UPDATE projects SET authz_project_id = ?, github_repo_id = ?, github_installation_id = ?, "
               "is_private = CASE WHEN ? THEN 1 ELSE is_private END, updated_at = ? WHERE id = ?",
               (authz_pid, int(reg["github_repo_id"]), installation_id, 1 if reg.get("private") else 0,
                utcnow(), p["id"]))
    db.commit()
    audit("project.authz_linked", actor_id=actor_id, target=p["slug"],
          detail=f"{how} {authz_pid} repo {reg['github_repo_id']} ({reg.get('verified_by')})", project_id=p["id"])


@bp.get("/authorization")
@admin_required
def authorization_status():
    """Whether the website is configured for authz. Never returns the key."""
    s = authz_client.authz_settings(current_app.config)
    return jsonify(configured=bool(s["url"] and s["key"]), required=s["required"],
                   runtime_credentials=bool(current_app.config.get("RUNTIME_SECRET_DIR")))


@bp.post("/projects/<slug>/authorization")
@admin_required
def link_authorization(slug):
    """Admin: link a website project to an EXISTING authz project (prj_...), or
    unlink it with an empty id. The repository is verified with GitHub by authz."""
    db = get_db()
    p = _project_by_slug(db, slug)
    if p is None:
        return jsonify(error="Project not found."), 404
    data = request.get_json(silent=True) or {}
    authz_pid = str(data.get("authz_project_id") or "").strip()
    if authz_pid and not AUTHZ_PROJECT_RE.match(authz_pid):
        return jsonify(error="authz_project_id must look like prj_<hex>."), 422
    try:
        inst = _int_or_none(data.get("github_installation_id"))
    except (TypeError, ValueError):
        return jsonify(error="github_installation_id must be a positive integer."), 422
    if not authz_pid:
        engine.revoke_project_credentials(db, current_app.config, p["id"], "authorization unlinked")
        db.execute("UPDATE projects SET authz_project_id = '', github_repo_id = NULL, "
                   "github_installation_id = NULL WHERE id = ?", (p["id"],))
        db.commit()
        audit("project.authz_unlinked", actor_id=current_user()["id"], target=slug, project_id=p["id"])
        return jsonify(ok=True, linked=False)
    client = authz_client.client_from_config(current_app.config)
    if client is None:
        return jsonify(error="Codeskys authorization is not configured on this server."), 503
    try:
        remote = client.get_project(authz_pid)
        if (remote.get("repo_url") or "").rstrip("/").lower() != p["repo_url"].rstrip("/").lower():
            return jsonify(error="That authorization project is bound to a different repository "
                                 "than this website project."), 409
        reg = client.register_repository(authz_pid, inst)
    except authz_client.AuthzError as exc:
        return _authz_error(exc)
    bind_authz_project(db, p, authz_pid, inst, reg, current_user()["id"], "admin-link")
    return jsonify(ok=True, linked=True, github_repo_id=reg["github_repo_id"], full_name=reg.get("full_name"),
                   private=bool(reg.get("private")), verified_by=reg.get("verified_by"))


@bp.post("/projects/<slug>/authorization/register")
@admin_required
def register_authorization(slug):
    """Admin: create a NEW authz project for a website project (new users only;
    the 48 existing student projects link with their credential instead)."""
    db = get_db()
    p = _project_by_slug(db, slug)
    if p is None:
        return jsonify(error="Project not found."), 404
    if p["authz_project_id"]:
        return jsonify(error="This project is already linked to Codeskys authorization."), 409
    data = request.get_json(silent=True) or {}
    try:
        inst = _int_or_none(data.get("github_installation_id"))
    except (TypeError, ValueError):
        return jsonify(error="github_installation_id must be a positive integer."), 422
    client = authz_client.client_from_config(current_app.config)
    if client is None:
        return jsonify(error="Codeskys authorization is not configured on this server."), 503
    owner = db.execute("SELECT name, email FROM users WHERE id = ?", (p["owner_id"],)).fetchone()
    try:
        created = client.create_project(student_name=owner["name"] if owner else "Codeskys user",
                                        email=owner["email"] if owner else "", project_name=p["name"],
                                        project_type=p["framework"], repo_url=p["repo_url"])
        authz_pid = str(created["project_id"])
        reg = client.register_repository(authz_pid, inst)
    except authz_client.AuthzError as exc:
        return _authz_error(exc)
    bind_authz_project(db, p, authz_pid, inst, reg, current_user()["id"], "admin-register")
    return jsonify(ok=True, linked=True, authz_project_id=authz_pid, github_repo_id=reg["github_repo_id"],
                   full_name=reg.get("full_name"), private=bool(reg.get("private"))), 201


@bp.post("/projects/<slug>/suspend")
@admin_required
def suspend_project(slug):
    """Suspend (or restore) a project everywhere: authz refuses its credentials
    within seconds, and the website pauses deployments."""
    db = get_db()
    p = _project_by_slug(db, slug)
    if p is None:
        return jsonify(error="Project not found."), 404
    data = request.get_json(silent=True) or {}
    suspended = bool(data.get("suspended", True))
    reason = str(data.get("reason") or "")[:200]
    if p["authz_project_id"]:
        client = authz_client.client_from_config(current_app.config)
        if client is None:
            return jsonify(error="Codeskys authorization is not configured on this server."), 503
        try:
            client.suspend_project(p["authz_project_id"], suspended, reason)
        except authz_client.AuthzError as exc:
            return _authz_error(exc)
    db.execute("UPDATE projects SET is_paused = ?, updated_at = ? WHERE id = ?",
               (1 if suspended else 0, utcnow(), p["id"]))
    db.commit()
    audit("project.suspended" if suspended else "project.restored", actor_id=current_user()["id"],
          target=slug, detail=reason, project_id=p["id"])
    return jsonify(ok=True, suspended=suspended)


@bp.get("/github/installations/<int:installation_id>/repositories")
@admin_required
def installation_repositories(installation_id):
    """Repository picker for a codeskys-deploy installation. Admin-only until the
    website can prove a user owns an installation (GitHub user authorization),
    otherwise anyone who guessed an installation id could list private repo names."""
    client = authz_client.client_from_config(current_app.config)
    if client is None:
        return jsonify(error="Codeskys authorization is not configured on this server."), 503
    try:
        out = client.installation_repositories(installation_id)
    except authz_client.AuthzError as exc:
        return _authz_error(exc)
    return jsonify(repository_selection=out.get("repository_selection"), total_count=out.get("total_count"),
                   repositories=[{k: r.get(k) for k in ("id", "full_name", "private", "default_branch")}
                                 for r in out.get("repositories", [])])


@bp.get("/integrations")
@admin_required
def integrations():
    return jsonify(_integration_view())


@bp.post("/integrations/coolify")
@admin_required
def save_coolify():
    data = request.get_json(silent=True) or {}
    db = get_db()
    url = str(data.get("url", "")).strip().rstrip("/")
    if url and not url.startswith(("https://", "http://")):
        return jsonify(error="Coolify URL must start with https://"), 400
    fields = {"coolify_url": url, "coolify_project_uuid": str(data.get("project_uuid", "")).strip(),
              "coolify_server_uuid": str(data.get("server_uuid", "")).strip(),
              "coolify_environment": str(data.get("environment_name") or "production").strip(),
              "coolify_base_domain": str(data.get("base_domain", "")).strip().lower().strip("."),
              "coolify_private_key_uuid": str(data.get("private_key_uuid", "")).strip(),
              "coolify_github_app_uuid": str(data.get("github_app_uuid", "")).strip(),
              "coolify_destination_uuid": str(data.get("destination_uuid", "")).strip()}
    if "force_https" in data:
        fields["coolify_force_https"] = "1" if data.get("force_https") else "0"
    for k, v in fields.items():
        set_setting(db, k, v)
    if data.get("token"):
        set_setting(db, "coolify_token", str(data["token"]).strip())
    if data.get("provider") in ("simulator", "coolify"):
        set_setting(db, "deploy_provider", data["provider"])
    audit("integration.coolify_saved", actor_id=current_user()["id"], target=url or "(cleared)")
    return jsonify(_integration_view())


@bp.post("/integrations/coolify/test")
@admin_required
def test_coolify():
    db = get_db()
    s = engine.coolify_settings(db, current_app.config)
    if not (s["url"] and s["token"]):
        return jsonify(error="Save a Coolify URL and API token first."), 400
    api = engine.Coolify(s["url"], s["token"], timeout=8)
    try:
        version = api.version()
        projects = api.projects()
        # A token scoped to one Coolify project (the recommended Codeskys token) is
        # refused (403) on team-level endpoints such as servers and keys.
        try:
            servers = api.servers()
        except engine.CoolifyError:
            servers = []
        try:
            keys = api.private_keys()
        except engine.CoolifyError:
            keys = []
    except engine.CoolifyError as exc:
        set_setting(db, "coolify_last_check", f"failed {utcnow()}")
        return jsonify(ok=False, error=exc.detail), 502
    v = version.get("raw") if isinstance(version, dict) and "raw" in version else version
    set_setting(db, "coolify_last_check", f"ok {utcnow()}")
    set_setting(db, "coolify_version", str(v)[:40])
    return jsonify(ok=True, version=v,
                   servers=[{"uuid": x.get("uuid"), "name": x.get("name"), "ip": x.get("ip")} for x in servers or []],
                   projects=[{"uuid": x.get("uuid"), "name": x.get("name")} for x in projects or []],
                   private_keys=[{"uuid": x.get("uuid"), "name": x.get("name")} for x in keys or []])


@bp.post("/integrations/provider")
@admin_required
def set_provider():
    provider = (request.get_json(silent=True) or {}).get("provider")
    if provider not in ("simulator", "coolify"):
        return jsonify(error="Provider must be simulator or coolify."), 400
    db = get_db()
    set_setting(db, "deploy_provider", provider)
    audit("integration.provider", actor_id=current_user()["id"], target=provider)
    view = _integration_view()
    if provider == "coolify" and view["active_provider"] != "coolify":
        view["warning"] = "Coolify is selected but not fully configured, so deployments still use the simulator."
    return jsonify(view)


# ---------------------------------------------------------------- platform operations

PLATFORM_KEYS = {
    "default_ttl_hours": ("Deployment lifetime (hours)", 1, 24 * 30),
    "max_projects_per_student": ("Projects per student", 1, 500),
}


@bp.get("/overview")
@admin_required
def platform_overview():
    """Everything an operator needs on one screen: usage, failures, queue, expiry."""
    db = get_db()
    now = utcnow()
    since = _hours_ago(24 * 30)
    stats = {
        "students": db.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'student'").fetchone()["n"],
        "admins": db.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'").fetchone()["n"],
        "inactive_users": db.execute("SELECT COUNT(*) AS n FROM users WHERE is_active = 0").fetchone()["n"],
        "projects": db.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"],
        "live": db.execute("SELECT COUNT(*) AS n FROM projects WHERE current_deployment_id IS NOT NULL").fetchone()["n"],
        "deploys_30d": db.execute("SELECT COUNT(*) AS n FROM deployments WHERE created_at >= ?", (since,)).fetchone()["n"],
        "failed_30d": db.execute("SELECT COUNT(*) AS n FROM deployments WHERE status = 'failed' AND created_at >= ?",
                                 (since,)).fetchone()["n"],
        "expired_30d": db.execute("SELECT COUNT(*) AS n FROM deployments WHERE status = 'expired' AND created_at >= ?",
                                  (since,)).fetchone()["n"],
        "in_flight": db.execute("SELECT COUNT(*) AS n FROM deployments WHERE status IN ('queued','building')").fetchone()["n"],
        "build_minutes_30d": round((db.execute(
            "SELECT COALESCE(SUM(duration_ms),0) AS t FROM deployments WHERE created_at >= ?",
            (since,)).fetchone()["t"] or 0) / 60000, 1),
    }
    queue = {row["state"]: row["n"] for row in db.execute(
        "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state").fetchall()}
    expiring = db.execute(
        "SELECT d.id, d.number, d.expires_at, p.name AS pname, p.slug AS pslug FROM deployments d "
        "JOIN projects p ON p.id = d.project_id WHERE d.status = 'ready' AND d.expires_at IS NOT NULL "
        "ORDER BY d.expires_at LIMIT 10").fetchall()
    failures = db.execute(
        "SELECT d.id, d.number, d.error, d.created_at, p.name AS pname, p.slug AS pslug, u.name AS owner "
        "FROM deployments d JOIN projects p ON p.id = d.project_id LEFT JOIN users u ON u.id = p.owner_id "
        "WHERE d.status = 'failed' ORDER BY d.id DESC LIMIT 15").fetchall()
    top = db.execute(
        "SELECT u.id, u.name, u.email, COUNT(DISTINCT p.id) AS projects, COUNT(d.id) AS deploys "
        "FROM users u LEFT JOIN projects p ON p.owner_id = u.id LEFT JOIN deployments d ON d.project_id = p.id "
        "GROUP BY u.id ORDER BY deploys DESC LIMIT 10").fetchall()
    return jsonify(
        stats=stats, queue=queue, now=now,
        provider=engine.active_provider(db, current_app.config),
        expiring=[{"id": r["id"], "number": r["number"], "expires_at": r["expires_at"],
                   "project": r["pname"], "slug": r["pslug"]} for r in expiring],
        failures=[{"id": r["id"], "number": r["number"], "error": r["error"], "created_at": r["created_at"],
                   "project": r["pname"], "slug": r["pslug"], "owner": r["owner"]} for r in failures],
        usage=[{"id": r["id"], "name": r["name"], "email": r["email"], "projects": r["projects"],
                "deploys": r["deploys"]} for r in top],
        settings=_platform_settings(db),
    )


def _hours_ago(hours):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _platform_settings(db):
    return {
        "default_ttl_hours": int(get_setting(db, "default_ttl_hours", "")
                                 or current_app.config.get("DEFAULT_TTL_HOURS", 24)),
        "max_projects_per_student": int(get_setting(db, "max_projects_per_student", "") or 0),
        "build_memory": current_app.config.get("BUILD_MEMORY", "512M"),
        "build_cpus": current_app.config.get("BUILD_CPUS", "0.5"),
        "inline_worker": bool(current_app.config.get("INLINE_WORKER", True)),
    }


@bp.get("/settings")
@admin_required
def get_platform_settings():
    return jsonify(settings=_platform_settings(get_db()))


@bp.post("/settings")
@admin_required
def save_platform_settings():
    data = request.get_json(silent=True) or {}
    db = get_db()
    for key, (label, low, high) in PLATFORM_KEYS.items():
        if key not in data:
            continue
        try:
            value = int(data[key])
        except (TypeError, ValueError):
            return jsonify(error=f"{label} must be a whole number."), 400
        if not low <= value <= high:
            return jsonify(error=f"{label} must be between {low} and {high}."), 400
        set_setting(db, key, str(value))
    audit("platform.settings", actor_id=current_user()["id"],
          detail=", ".join(k for k in data if k in PLATFORM_KEYS))
    return jsonify(settings=_platform_settings(db))


@bp.get("/deployments")
@admin_required
def all_deployments():
    """Every deployment on the platform, filterable — including expired ones."""
    db = get_db()
    status = request.args.get("status")
    where, args = [], []
    if status in ("queued", "building", "ready", "failed", "cancelled", "expired"):
        where.append("d.status = ?")
        args.append(status)
    owner = request.args.get("owner", type=int)
    if owner:
        where.append("p.owner_id = ?")
        args.append(owner)
    sql = ("SELECT d.*, p.name AS pname, p.slug AS pslug, p.owner_id, u.name AS owner_name "
           "FROM deployments d JOIN projects p ON p.id = d.project_id LEFT JOIN users u ON u.id = p.owner_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = db.execute(sql + " ORDER BY d.created_at DESC, d.id DESC LIMIT 300", args).fetchall()
    return jsonify(deployments=[{
        "id": r["id"], "number": r["number"], "status": r["status"], "target": r["target"],
        "branch": r["branch"], "provider": r["provider"], "is_simulated": r["provider"] == engine.SIMULATOR,
        "created_at": r["created_at"], "expires_at": r["expires_at"], "expired_at": r["expired_at"],
        "duration_ms": r["duration_ms"], "error": r["error"], "teardown_note": r["teardown_note"],
        "project": r["pname"], "slug": r["pslug"], "owner": r["owner_name"],
    } for r in rows])


@bp.post("/deployments/<int:dep_id>/expire")
@admin_required
def force_expire(dep_id):
    """Expire a deployment now: tear down its Coolify application and mark it expired."""
    db = get_db()
    dep = db.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    if dep is None:
        return jsonify(error="Deployment not found."), 404
    if dep["expired_at"]:
        return jsonify(error="That deployment has already expired."), 409
    if dep["status"] in ("queued", "building"):
        engine.request_cancel(db, dep_id)
        audit("deploy.cancelled", actor_id=current_user()["id"], target=f"#{dep['number']}",
              detail="stopped by administrator", project_id=dep["project_id"])
        return jsonify(ok=True, action="cancelled")
    if dep["status"] != "ready":
        return jsonify(error="Only a live deployment can be expired."), 409
    engine.expire_deployment(db, current_app.config, {"deployment_id": dep_id}, lambda *a, **k: True)
    audit("deploy.expired", actor_id=current_user()["id"], target=f"#{dep['number']}",
          detail="expired by administrator", project_id=dep["project_id"])
    fresh = db.execute("SELECT status, teardown_note FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    return jsonify(ok=True, action="expired", status=fresh["status"], note=fresh["teardown_note"])


@bp.get("/queue")
@admin_required
def queue_view():
    db = get_db()
    rows = db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify(jobs=[{
        "id": r["id"], "kind": r["kind"], "state": r["state"], "attempts": r["attempts"],
        "max_attempts": r["max_attempts"], "run_after": r["run_after"], "locked_by": r["locked_by"],
        "locked_until": r["locked_until"], "last_error": r["last_error"], "created_at": r["created_at"],
    } for r in rows])


@bp.post("/queue/<int:job_id>/retry")
@admin_required
def retry_job(job_id):
    db = get_db()
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return jsonify(error="Job not found."), 404
    if row["state"] not in ("failed", "cancelled"):
        return jsonify(error="Only a failed job can be retried."), 409
    db.execute("UPDATE jobs SET state='pending', attempts=0, run_after=?, last_error=NULL, updated_at=? "
               "WHERE id = ?", (utcnow(), utcnow(), job_id))
    db.commit()
    audit("queue.retried", actor_id=current_user()["id"], target=f"job {job_id}")
    return jsonify(ok=True)


@bp.get("/projects")
@admin_required
def all_projects():
    db = get_db()
    rows = db.execute(
        "SELECT p.*, u.name AS owner_name, u.email AS owner_email, "
        "(SELECT COUNT(*) FROM deployments d WHERE d.project_id = p.id) AS deploys "
        "FROM projects p LEFT JOIN users u ON u.id = p.owner_id ORDER BY p.updated_at DESC").fetchall()
    return jsonify(projects=[{
        "id": r["id"], "name": r["name"], "slug": r["slug"], "repo_full_name": r["repo_full_name"],
        "framework": r["framework"], "is_paused": bool(r["is_paused"]), "is_private": bool(r["is_private"]),
        "git_provider": r["git_provider"], "ttl_hours": r["ttl_hours"], "deploys": r["deploys"],
        "live": r["current_deployment_id"] is not None, "owner": r["owner_name"], "owner_email": r["owner_email"],
        "limits_memory": r["limits_memory"], "limits_cpus": r["limits_cpus"], "updated_at": r["updated_at"],
    } for r in rows])
