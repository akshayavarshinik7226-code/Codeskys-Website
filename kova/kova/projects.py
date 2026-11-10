"""Projects, deployments, environment variables, domains, deploy hooks and analytics."""
import re
import secrets
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, current_app, jsonify, request

from . import engine
from .crypto import secrets_for
from .auth import current_user, login_required
from .db import audit, get_db, utcnow
from .gitinfo import FRAMEWORKS, GitError, inspect_repo, parse_repo
from .providers import account_for, token_for, token_for_project
from .ratelimit import rate_limit

bp = Blueprint("projects", __name__, url_prefix="/api")

SLUG_RE = re.compile(r"[^a-z0-9-]+")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MEMORY_RE = re.compile(r"^\d{1,6}[KMG]$")
CPUS_RE = re.compile(r"^\d{1,3}(\.\d{1,2})?$")
MAX_TTL_HOURS = 24 * 30
MAX_PROJECTS = {"hobby": 1, "pro": 100, "team": 1000}
PLAN_LIMITS = {
    "hobby": {"projects": 1, "build_minutes": 300, "bandwidth_gb": 100, "concurrent": 1},
    "pro": {"projects": 100, "build_minutes": 3000, "bandwidth_gb": 1000, "concurrent": 3},
    "team": {"projects": 1000, "build_minutes": 25000, "bandwidth_gb": 5000, "concurrent": 10},
    "premium": {"projects": 1000, "build_minutes": 25000, "bandwidth_gb": 5000, "concurrent": 10},
}


def err(message, code=400):
    return jsonify(error=message), code


def slugify(text: str) -> str:
    s = SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return re.sub("-{2,}", "-", s)[:48] or "project"


def _unique_slug(db, base):
    slug, n = base, 2
    while db.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _load_project(slug):
    user = current_user()
    row = get_db().execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    if row is None or (user["role"] != "admin" and row["owner_id"] != user["id"]):
        return None
    return row


def _dep_public(d):
    if d is None:
        return None
    return {k: d[k] for k in ("id", "number", "status", "stage", "target", "branch", "commit_sha", "commit_message",
                              "trigger", "url", "provider", "error", "created_at", "started_at", "finished_at",
                              "duration_ms", "expires_at", "expired_at", "teardown_note")} | {
        "project_id": d["project_id"],
        "authz_deployment_id": d["authz_deployment_id"] or None,
        "credential_revoked": bool(d["authz_revoked_at"]),
        # The UI must never present a simulated build as a real one.
        "is_simulated": d["provider"] == engine.SIMULATOR,
    }


def _project_public(p, db=None, full=False):
    db = db or get_db()
    current = db.execute("SELECT * FROM deployments WHERE id = ?", (p["current_deployment_id"],)).fetchone() \
        if p["current_deployment_id"] else None
    latest = db.execute("SELECT * FROM deployments WHERE project_id = ? ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
    owner = db.execute("SELECT name, email FROM users WHERE id = ?", (p["owner_id"],)).fetchone()
    primary = db.execute("SELECT hostname FROM domains WHERE project_id = ? AND is_primary = 1 AND status = 'active'",
                         (p["id"],)).fetchone()
    out = {
        "id": p["id"], "name": p["name"], "slug": p["slug"], "description": p["description"],
        "repo_url": p["repo_url"], "repo_full_name": p["repo_full_name"], "branch": p["branch"],
        "framework": p["framework"], "framework_name": FRAMEWORKS.get(p["framework"], FRAMEWORKS["static"])["name"],
        "is_paused": bool(p["is_paused"]), "provider": p["provider"], "created_at": p["created_at"],
        "updated_at": p["updated_at"], "owner": {"name": owner["name"], "email": owner["email"]} if owner else None,
        "production": _dep_public(current), "latest": _dep_public(latest),
        "primary_domain": primary["hostname"] if primary else None,
        "deploy_count": db.execute("SELECT COUNT(*) AS n FROM deployments WHERE project_id = ?", (p["id"],)).fetchone()["n"],
    }
    out["git_provider"] = p["git_provider"]
    out["is_private"] = bool(p["is_private"])
    out["authorization"] = {
        "linked": bool(p["authz_project_id"]),
        "github_repo_id": p["github_repo_id"],
        "github_app_installation": bool(p["github_installation_id"]),
    }
    out["app_state"] = p["app_state"] or None
    out["ttl_hours"] = p["ttl_hours"]
    if full:
        out.update({k: p[k] for k in ("root_dir", "install_command", "build_command", "output_dir", "start_command",
                                      "port", "node_version", "provider_ref", "limits_memory", "limits_cpus")})
        out["auto_deploy"] = bool(p["auto_deploy"])
        out["preview_deploys"] = bool(p["preview_deploys"])
        base = request.host_url.rstrip("/")
        # The webhook secret itself is never returned — only the URLs that carry the id.
        out["hooks"] = {
            "generic": f"{base}/api/hooks/deploy/{p['hook_token']}",
            "github": f"{base}/api/hooks/github/{p['hook_token']}",
            "gitlab": f"{base}/api/hooks/gitlab/{p['hook_token']}",
            "secret_set": bool(p["webhook_secret"]),
        }
        out["hook_url"] = out["hooks"]["generic"]
        linked = db.execute("SELECT provider, username FROM git_accounts WHERE id = ?",
                            (p["git_account_id"],)).fetchone() if p["git_account_id"] else None
        out["git_account"] = {"provider": linked["provider"], "username": linked["username"]} if linked else None
        fw = FRAMEWORKS.get(p["framework"], FRAMEWORKS["static"])
        out["defaults"] = {"install_command": fw["install"], "build_command": fw["build"],
                           "output_dir": fw["output"], "start_command": fw["start"]}
    return out


def trigger_deployment(db, project, *, target="production", branch=None, trigger="manual", user_id=None,
                       commit_sha=None, commit_message=None):
    """Create a deployment row and hand it to the durable queue.

    Shared by the API, the webhooks and the CI hook, so every path gets the same
    pause checks, provider selection and queueing behaviour.
    """
    if project["is_paused"]:
        raise GitError("This project is paused. Resume it in Settings to deploy.")
    branch = branch or project["branch"]
    if commit_sha is None:
        if current_app.config.get("GIT_LOOKUPS", True):
            token = token_for_project(db, project)
            commit_sha, msg = engine.resolve_commit(project, branch, token=token)
        else:
            commit_sha, msg = secrets.token_hex(20), ""
        commit_message = commit_message or msg or f"Deploy {branch}"
    number = db.execute("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM deployments WHERE project_id = ?",
                        (project["id"],)).fetchone()["n"]
    provider = engine.active_provider(db, current_app.config)
    cur = db.execute(
        "INSERT INTO deployments (project_id, number, status, stage, target, branch, commit_sha, commit_message, "
        "trigger, triggered_by, provider, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (project["id"], number, "queued", "queued", target, branch, commit_sha, commit_message or "", trigger,
         user_id, provider, utcnow()))
    db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utcnow(), project["id"]))
    db.commit()
    dep_id = cur.lastrowid
    db.execute("INSERT INTO deploy_logs (deployment_id, ts, level, stage, line) VALUES (?,?,?,?,?)",
               (dep_id, utcnow(), "info", "queued",
                f"Deployment #{number} queued ({trigger}, {target}, branch {branch}) on the "
                f"{'Coolify' if provider == engine.COOLIFY else 'simulator'} engine"))
    db.commit()
    engine.enqueue_deploy(db, dep_id)
    return dep_id


# Backwards-compatible alias used by older call sites.
_trigger = trigger_deployment


# ------------------------------------------------------------------ catalog / git

@bp.get("/frameworks")
@login_required
def frameworks():
    return jsonify(frameworks=[{"id": k, **v} for k, v in FRAMEWORKS.items()])


@bp.get("/git/inspect")
@login_required
def git_inspect():
    url = request.args.get("url", "")
    try:
        if not current_app.config.get("GIT_LOOKUPS", True):
            info = parse_repo(url)
            return jsonify(repo={**info, "default_branch": "main", "branches": ["main"], "framework": "static",
                                 "reachable": None, "description": ""})
        return jsonify(repo=inspect_repo(url))
    except GitError as exc:
        return err(str(exc), 422)


# ------------------------------------------------------------------ overview

@bp.get("/overview")
@login_required
def overview():
    db = get_db()
    user = current_user()
    scope, args = ("", ()) if user["role"] == "admin" else (" WHERE p.owner_id = ?", (user["id"],))
    projects = db.execute(f"SELECT p.* FROM projects p{scope} ORDER BY p.updated_at DESC", args).fetchall()
    ids = [p["id"] for p in projects] or [-1]
    q = ",".join("?" * len(ids))
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    deps = db.execute(f"SELECT * FROM deployments WHERE project_id IN ({q}) AND created_at >= ? ORDER BY id DESC",
                      (*ids, since)).fetchall()
    done = [d for d in deps if d["status"] in ("ready", "failed")]
    ok = [d for d in done if d["status"] == "ready"]
    durations = [d["duration_ms"] for d in ok if d["duration_ms"]]
    recent = db.execute(f"SELECT d.*, p.name AS pname, p.slug AS pslug FROM deployments d JOIN projects p "
                        f"ON p.id = d.project_id WHERE d.project_id IN ({q}) ORDER BY d.created_at DESC, d.id DESC LIMIT 8", ids).fetchall()
    return jsonify(
        stats={
            "projects": len(projects),
            "live": sum(1 for p in projects if p["current_deployment_id"]),
            "deploys_30d": len(deps),
            "success_rate": round(100 * len(ok) / len(done), 1) if done else None,
            "avg_build_ms": int(sum(durations) / len(durations)) if durations else None,
            "building": sum(1 for d in deps if d["status"] in ("queued", "building")),
            "build_minutes": round(sum(d["duration_ms"] or 0 for d in deps) / 60000, 1),
        },
        daily=_daily(deps, 14),
        projects=[_project_public(p, db) for p in projects[:6]],
        recent=[_dep_public(d) | {"project_name": d["pname"], "project_slug": d["pslug"]} for d in recent],
        provider=engine.active_provider(db, current_app.config),
        plan=user["plan"], limits=PLAN_LIMITS.get(user["plan"], PLAN_LIMITS["hobby"]),
    )


def _daily(deps, days):
    today = datetime.now(timezone.utc).date()
    buckets = {(today - timedelta(days=i)).isoformat(): {"ready": 0, "failed": 0, "other": 0} for i in range(days)}
    for d in deps:
        day = d["created_at"][:10]
        if day in buckets:
            key = d["status"] if d["status"] in ("ready", "failed") else "other"
            buckets[day][key] += 1
    return [{"date": k, **v} for k, v in sorted(buckets.items())]


# ------------------------------------------------------------------ projects

@bp.get("/projects")
@login_required
def list_projects():
    db = get_db()
    user = current_user()
    if user["role"] == "admin" and request.args.get("scope") != "mine":
        rows = db.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
    else:
        rows = db.execute("SELECT * FROM projects WHERE owner_id = ? ORDER BY updated_at DESC", (user["id"],)).fetchall()
    return jsonify(projects=[_project_public(p, db) for p in rows])


@bp.post("/projects")
@login_required
def create_project():
    data = request.get_json(silent=True) or {}
    user = current_user()
    db = get_db()
    try:
        repo = parse_repo(data.get("repo_url", ""))
    except GitError as exc:
        return err(str(exc), 422)
    name = str(data.get("name") or repo["repo"]).strip()[:64]
    if not name:
        return err("Give your project a name.")
    limit = PLAN_LIMITS.get(user["plan"], PLAN_LIMITS["hobby"])["projects"]
    if user["role"] != "admin":
        count = db.execute("SELECT COUNT(*) AS n FROM projects WHERE owner_id = ?", (user["id"],)).fetchone()["n"]
        if count >= limit:
            return err(f"Your {user['plan'].title()} plan allows {limit} projects. Delete one or upgrade.", 402)
    framework = data.get("framework") or "static"
    if framework not in FRAMEWORKS:
        return err("Unknown framework preset.")
    slug = _unique_slug(db, slugify(data.get("slug") or name))
    now = utcnow()
    fields = {k: str(data.get(k) or "").strip()[:300] for k in
              ("install_command", "build_command", "output_dir", "start_command", "root_dir")}
    try:
        port = int(data.get("port") or (FRAMEWORKS[framework]["port"]))
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return err("Port must be a whole number between 1 and 65535.")
    ttl, ttl_err = _clean_ttl(data.get("ttl_hours"), db)
    if ttl_err:
        return err(ttl_err)

    # A private repository can only be deployed through a connected account.
    is_private = bool(data.get("is_private"))
    git_provider = data.get("git_provider") or repo["provider"]
    if git_provider not in ("github", "gitlab"):
        git_provider = "github" if repo["provider"] == "git" else repo["provider"]
    account = account_for(db, user["id"], git_provider) if git_provider in ("github", "gitlab") else None
    if is_private and account is None:
        return err(f"This repository is private. Connect your {git_provider.title()} account first.", 409)

    cur = db.execute(
        "INSERT INTO projects (owner_id, name, slug, description, repo_url, repo_full_name, branch, framework, root_dir, "
        "install_command, build_command, output_dir, start_command, port, node_version, hook_token, created_at, updated_at, "
        "git_account_id, git_provider, repo_external_id, is_private, webhook_secret, ttl_hours, limits_memory, limits_cpus) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], name, slug, str(data.get("description") or "")[:300], repo["url"], repo["full_name"],
         str(data.get("branch") or "main").strip()[:100], framework, fields["root_dir"].strip("/"),
         fields["install_command"], fields["build_command"], fields["output_dir"], fields["start_command"], port,
         str(data.get("node_version") or "20.x"), secrets.token_urlsafe(24), now, now,
         account["id"] if account else None, git_provider, str(data.get("repo_external_id") or "")[:64],
         1 if is_private else 0, secrets.token_urlsafe(32), ttl,
         current_app.config.get("BUILD_MEMORY", "512M"), current_app.config.get("BUILD_CPUS", "0.5")))
    pid = cur.lastrowid
    box = secrets_for(current_app)
    for env in (data.get("env") or [])[:200]:
        key = str(env.get("key", "")).strip()
        if ENV_KEY_RE.match(key):
            value = str(env.get("value", ""))[:8000]
            cipher = box.encrypt(value)
            db.execute("INSERT OR REPLACE INTO env_vars (project_id, key, value, value_cipher, is_secret, targets, created_at, updated_at) "
                       "VALUES (?,?,?,?,1,?,?,?)", (pid, key, "", cipher, "production,preview", now, now))
    db.execute("INSERT INTO domains (project_id, hostname, is_primary, is_system, status, verify_token, created_at, "
               "verified_at) VALUES (?,?,?,?,?,?,?,?)",
               (pid, f"{slug}.{_system_domain(db)}", 1, 1, "active", secrets.token_hex(8), now, now))
    db.commit()
    audit("project.created", actor_id=user["id"], target=slug, ip=request.remote_addr, project_id=pid)
    project = db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    dep_id = None
    if data.get("deploy", True):
        dep_id = trigger_deployment(db, project, trigger="initial", user_id=user["id"])
        project = db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    return jsonify(project=_project_public(project, db, full=True), deployment_id=dep_id), 201


def _system_domain(db):
    from .db import get_setting
    return get_setting(db, "coolify_base_domain", "") or current_app.config.get("SYSTEM_DOMAIN", "codeskys.com")


def _clean_ttl(value, db):
    """Deployment lifetime in hours. Falls back to the platform default."""
    from .db import get_setting
    if value in (None, ""):
        try:
            return int(get_setting(db, "default_ttl_hours", "") or current_app.config.get("DEFAULT_TTL_HOURS", 24)), None
        except (TypeError, ValueError):
            return 24, None
    try:
        ttl = int(value)
    except (TypeError, ValueError):
        return None, "Lifetime must be a whole number of hours."
    if not 1 <= ttl <= MAX_TTL_HOURS:
        return None, f"Lifetime must be between 1 and {MAX_TTL_HOURS} hours."
    return ttl, None


@bp.get("/projects/<slug>")
@login_required
def get_project(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    return jsonify(project=_project_public(p, full=True))


@bp.patch("/projects/<slug>")
@login_required
def update_project(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    data = request.get_json(silent=True) or {}
    db = get_db()
    updates = {}
    for key in ("name", "description", "branch", "root_dir", "install_command", "build_command", "output_dir",
                "start_command", "node_version"):
        if key in data:
            updates[key] = str(data[key] or "").strip()[:300]
    if "name" in updates and not updates["name"]:
        return err("Name cannot be empty.")
    if "branch" in updates and not updates["branch"]:
        return err("Branch cannot be empty.")
    if "framework" in data:
        if data["framework"] not in FRAMEWORKS:
            return err("Unknown framework preset.")
        updates["framework"] = data["framework"]
    if "port" in data:
        try:
            port = int(data["port"])
            assert 1 <= port <= 65535
        except (TypeError, ValueError, AssertionError):
            return err("Port must be between 1 and 65535.")
        updates["port"] = port
    for flag in ("auto_deploy", "preview_deploys", "is_paused"):
        if flag in data:
            updates[flag] = 1 if data[flag] else 0
    if "ttl_hours" in data:
        ttl, ttl_err = _clean_ttl(data["ttl_hours"], db)
        if ttl_err:
            return err(ttl_err)
        updates["ttl_hours"] = ttl
    # Build resource limits are admin-only: a student must not be able to hand
    # themselves the whole server.
    if current_user()["role"] == "admin":
        if "limits_memory" in data:
            if not MEMORY_RE.match(str(data["limits_memory"]).strip()):
                return err("Memory limit must look like 512M or 2G.")
            updates["limits_memory"] = str(data["limits_memory"]).strip()
        if "limits_cpus" in data:
            if not CPUS_RE.match(str(data["limits_cpus"]).strip()):
                return err("CPU limit must be a number such as 0.5 or 2.")
            updates["limits_cpus"] = str(data["limits_cpus"]).strip()
    elif "limits_memory" in data or "limits_cpus" in data:
        return err("Only an administrator can change build resource limits.", 403)
    if "repo_url" in data:
        try:
            repo = parse_repo(data["repo_url"])
        except GitError as exc:
            return err(str(exc), 422)
        updates["repo_url"], updates["repo_full_name"] = repo["url"], repo["full_name"]
        if repo["url"] != p["repo_url"]:
            # A different repository must be verified with GitHub again before the
            # next real deployment; the old numeric id is not carried over.
            updates["github_repo_id"] = None
    if not updates:
        return err("Nothing to update.")
    updates["updated_at"] = utcnow()
    cols = ", ".join(f"{k} = ?" for k in updates)
    db.execute(f"UPDATE projects SET {cols} WHERE id = ?", (*updates.values(), p["id"]))
    db.commit()
    audit("project.updated", actor_id=current_user()["id"], target=slug, ip=request.remote_addr,
          detail=", ".join(k for k in updates if k != "updated_at"), project_id=p["id"])
    return jsonify(project=_project_public(db.execute("SELECT * FROM projects WHERE id = ?", (p["id"],)).fetchone(),
                                           db, full=True))


@bp.delete("/projects/<slug>")
@login_required
def delete_project(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != p["slug"]:
        return err("Type the project name to confirm deletion.")
    db = get_db()
    # Credentials first: even if the app teardown below fails, nothing deployed
    # for this project can reach protected Codeskys services any more.
    notes = engine.revoke_project_credentials(db, current_app.config, p["id"], "project deleted")
    pending = [n for n in notes if "could not be revoked" in n]
    if pending and not data.get("force"):
        return err("Codeskys could not revoke this project's deployment credentials, so it was not "
                   "deleted. Try again shortly, or ask an administrator.", 503)
    if p["provider_ref"]:
        try:
            s = engine.coolify_settings(db, current_app.config)
            if s["url"] and s["token"]:
                engine.Coolify(s["url"], s["token"]).delete_app(p["provider_ref"])
        except engine.CoolifyError as exc:
            current_app.logger.warning("Coolify delete failed: %s", exc)
    db.execute("DELETE FROM projects WHERE id = ?", (p["id"],))
    db.commit()
    audit("project.deleted", actor_id=current_user()["id"], target=slug, ip=request.remote_addr)
    return jsonify(ok=True)


@bp.post("/projects/<slug>/hook/rotate")
@login_required
def rotate_hook(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    db = get_db()
    db.execute("UPDATE projects SET hook_token = ? WHERE id = ?", (secrets.token_urlsafe(24), p["id"]))
    db.commit()
    audit("project.hook_rotated", actor_id=current_user()["id"], target=slug, project_id=p["id"])
    return get_project(slug)


@bp.post("/projects/<slug>/transfer")
@login_required
def transfer_project(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    email = str((request.get_json(silent=True) or {}).get("email", "")).strip().lower()
    db = get_db()
    target = db.execute("SELECT id FROM users WHERE email = ? AND is_active = 1", (email,)).fetchone()
    if not target:
        return err("No active account uses that email.", 404)
    db.execute("UPDATE projects SET owner_id = ?, updated_at = ? WHERE id = ?", (target["id"], utcnow(), p["id"]))
    db.commit()
    audit("project.transferred", actor_id=current_user()["id"], target=email, project_id=p["id"])
    return jsonify(ok=True)


# ------------------------------------------------------------------ deployments

@bp.post("/projects/<slug>/deploy")
@login_required
def deploy(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    data = request.get_json(silent=True) or {}
    target = data.get("target", "production")
    if target not in ("production", "preview"):
        return err("Target must be production or preview.")
    db = get_db()
    user = current_user()
    if rate_limit(db, f"deploy:{user['id']}", limit=30, per_seconds=300):
        return err("You're deploying very frequently. Wait a moment and try again.", 429)
    branch = str(data.get("branch") or "").strip()[:200]
    try:
        dep_id = trigger_deployment(db, p, target=target, branch=(branch or None),
                                    trigger="redeploy-clear-cache" if data.get("clear_cache") else "manual",
                                    user_id=user["id"])
    except GitError as exc:
        return err(str(exc), 409)
    audit("deploy.triggered", actor_id=current_user()["id"], target=f"#{dep_id}", project_id=p["id"])
    return jsonify(deployment=_dep_public(db.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone())), 201


@bp.get("/projects/<slug>/deployments")
@login_required
def project_deployments(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    rows = get_db().execute("SELECT * FROM deployments WHERE project_id = ? ORDER BY id DESC LIMIT 200",
                            (p["id"],)).fetchall()
    return jsonify(deployments=[_dep_public(d) for d in rows], current_id=p["current_deployment_id"])


@bp.get("/deployments")
@login_required
def all_deployments():
    user = current_user()
    db = get_db()
    where, args = [], []
    if user["role"] != "admin":
        where.append("p.owner_id = ?")
        args.append(user["id"])
    status = request.args.get("status")
    if status in ("queued", "building", "ready", "failed", "cancelled"):
        where.append("d.status = ?")
        args.append(status)
    sql = ("SELECT d.*, p.name AS pname, p.slug AS pslug, p.current_deployment_id AS cur FROM deployments d "
           "JOIN projects p ON p.id = d.project_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = db.execute(sql + " ORDER BY d.created_at DESC, d.id DESC LIMIT 300", args).fetchall()
    return jsonify(deployments=[_dep_public(d) | {"project_name": d["pname"], "project_slug": d["pslug"],
                                                 "is_current": d["cur"] == d["id"]} for d in rows])


def _load_deployment(dep_id):
    d = get_db().execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    if d is None:
        return None, None
    p = get_db().execute("SELECT * FROM projects WHERE id = ?", (d["project_id"],)).fetchone()
    user = current_user()
    if user["role"] != "admin" and p["owner_id"] != user["id"]:
        return None, None
    return d, p


@bp.get("/deployments/<int:dep_id>")
@login_required
def get_deployment(dep_id):
    d, p = _load_deployment(dep_id)
    if d is None:
        return err("Deployment not found.", 404)
    after = request.args.get("after", 0, type=int)
    logs = get_db().execute("SELECT id, ts, level, stage, line FROM deploy_logs WHERE deployment_id = ? AND id > ? "
                            "ORDER BY id LIMIT 2000", (dep_id, after)).fetchall()
    user = get_db().execute("SELECT name FROM users WHERE id = ?", (d["triggered_by"],)).fetchone() \
        if d["triggered_by"] else None
    return jsonify(deployment=_dep_public(d) | {"is_current": p["current_deployment_id"] == d["id"],
                                                "triggered_by": user["name"] if user else None,
                                                "project_slug": p["slug"], "project_name": p["name"],
                                                "repo_url": p["repo_url"]},
                   logs=[dict(r) for r in logs])


@bp.post("/deployments/<int:dep_id>/cancel")
@login_required
def cancel_deployment(dep_id):
    d, p = _load_deployment(dep_id)
    if d is None:
        return err("Deployment not found.", 404)
    if d["status"] not in ("queued", "building"):
        return err("Only queued or building deployments can be cancelled.", 409)
    # Signalled through the database so it reaches the worker whatever process it runs in.
    engine.request_cancel(get_db(), dep_id)
    audit("deploy.cancelled", actor_id=current_user()["id"], target=f"#{d['number']}", project_id=p["id"])
    return jsonify(ok=True)


@bp.post("/deployments/<int:dep_id>/redeploy")
@login_required
def redeploy(dep_id):
    d, p = _load_deployment(dep_id)
    if d is None:
        return err("Deployment not found.", 404)
    db = get_db()
    try:
        new_id = trigger_deployment(db, p, target=d["target"], branch=d["branch"], trigger="redeploy",
                                    user_id=current_user()["id"], commit_sha=d["commit_sha"],
                                    commit_message=d["commit_message"])
    except GitError as exc:
        return err(str(exc), 409)
    return jsonify(deployment=_dep_public(db.execute("SELECT * FROM deployments WHERE id = ?", (new_id,)).fetchone())), 201


@bp.post("/deployments/<int:dep_id>/promote")
@login_required
def promote(dep_id):
    """Instant rollback / promote: point production at an earlier successful build."""
    d, p = _load_deployment(dep_id)
    if d is None:
        return err("Deployment not found.", 404)
    if d["status"] != "ready":
        return err("Only successful deployments can be published.", 409)
    db = get_db()
    if d["status"] == "expired":
        return err("This deployment has expired. Redeploy the project instead.", 409)
    if d["provider"] == engine.COOLIFY:
        try:
            new_id = trigger_deployment(db, p, target="production", branch=d["branch"], trigger="rollback",
                                        user_id=current_user()["id"], commit_sha=d["commit_sha"],
                                        commit_message=f"Rollback to #{d['number']}: {d['commit_message']}")
        except GitError as exc:
            return err(str(exc), 409)
        return jsonify(deployment_id=new_id, mode="rebuild")
    db.execute("UPDATE projects SET current_deployment_id = ?, updated_at = ? WHERE id = ?", (d["id"], utcnow(), p["id"]))
    db.commit()
    audit("deploy.promoted", actor_id=current_user()["id"], target=f"#{d['number']}", project_id=p["id"])
    return jsonify(deployment_id=d["id"], mode="instant")


# ------------------------------------------------------------------ live application controls

_APP_ACTIONS = {"start": "start_app", "stop": "stop_app", "restart": "restart_app"}


def _coolify_app(p):
    """(api, uuid) for a project that has a real Coolify application, else an error response."""
    if not p["provider_ref"] or p["provider"] != engine.COOLIFY:
        return None, err("This project has no real deployment to control yet "
                         "(simulated deployments cannot be started or stopped).", 409)
    try:
        api, _ = engine.client_for(get_db(), current_app.config)
    except engine.CoolifyError as exc:
        return None, err(str(exc), 503)
    return api, None


@bp.get("/projects/<slug>/app")
@login_required
def app_status(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    api, error = _coolify_app(p)
    if error:
        return error
    try:
        remote = api.get_app(p["provider_ref"])
    except engine.CoolifyError as exc:
        return err(f"Could not read the application status: {exc}", 502)
    status = str(remote.get("status") or "unknown")
    get_db().execute("UPDATE projects SET app_state = ? WHERE id = ?", (status[:40], p["id"]))
    get_db().commit()
    return jsonify(status=status, fqdn=(remote.get("fqdn") or "").split(",")[0] or None)


@bp.post("/projects/<slug>/app/<action>")
@login_required
def app_action(slug, action):
    if action not in _APP_ACTIONS:
        return err("Action must be start, stop or restart.", 404)
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    if action in ("start", "restart") and p["is_paused"]:
        return err("This project is paused. Resume it in Settings first.", 409)
    db = get_db()
    user = current_user()
    if rate_limit(db, f"app:{user['id']}", limit=20, per_seconds=300):
        return err("Too many start/stop requests. Wait a moment and try again.", 429)
    api, error = _coolify_app(p)
    if error:
        return error
    try:
        getattr(api, _APP_ACTIONS[action])(p["provider_ref"])
    except engine.CoolifyError as exc:
        return err(f"Coolify could not {action} the application: {exc}", 502)
    state = {"start": "starting", "stop": "stopped", "restart": "restarting"}[action]
    db.execute("UPDATE projects SET app_state = ?, updated_at = ? WHERE id = ?", (state, utcnow(), p["id"]))
    db.commit()
    audit(f"app.{action}", actor_id=user["id"], target=slug, ip=request.remote_addr, project_id=p["id"])
    return jsonify(ok=True, app_state=state)


# ------------------------------------------------------------------ Codeskys authorization (owner)

@bp.post("/projects/<slug>/authorization/link")
@login_required
def link_my_authorization(slug):
    """Link this project to the student's EXISTING Codeskys authorization.

    The student proves ownership with their current ``DEPLOY_CREDENTIAL``. authz
    only verifies it (never rotates, stores or returns it); the website never
    stores or logs it either. Strictly rate-limited against guessing.
    """
    from .admin import bind_authz_project  # shared with the admin link flow
    from . import authz_client
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    db = get_db()
    user = current_user()
    if rate_limit(db, f"authz-link:{user['id']}", limit=5, per_seconds=900) or             rate_limit(db, f"authz-link-ip:{request.remote_addr}", limit=10, per_seconds=900):
        return err("Too many attempts. Wait 15 minutes and try again.", 429)
    data = request.get_json(silent=True) or {}
    credential = str(data.get("credential") or "").strip()
    if not credential or len(credential) > 200:
        return err("Enter your Codeskys deployment credential.", 422)
    try:
        inst = int(data["github_installation_id"]) if data.get("github_installation_id") else None
        if inst is not None and inst < 1:
            raise ValueError
    except (TypeError, ValueError):
        return err("The GitHub App installation id must be a positive number.", 422)
    client = authz_client.client_from_config(current_app.config)
    if client is None:
        return err("Codeskys authorization is not available on this server yet.", 503)
    try:
        linked = client.link_project(p["repo_url"], credential)
        authz_pid = str(linked["project_id"])
        reg = client.register_repository(authz_pid, inst)
    except authz_client.AuthzError as exc:
        audit("project.authz_link_failed", actor_id=user["id"], target=slug, ip=request.remote_addr,
              detail=exc.describe(), project_id=p["id"])
        status = exc.status if exc.status in (403, 404, 409, 422) else 502
        return err(exc.user_message(), status)
    finally:
        credential = None  # noqa: F841 - drop the only reference promptly
    bind_authz_project(db, p, authz_pid, inst, reg, user["id"], "owner-link")
    return jsonify(ok=True, linked=True, github_repo_id=reg["github_repo_id"], full_name=reg.get("full_name"),
                   private=bool(reg.get("private")))


# ------------------------------------------------------------------ environment variables

def _env_plaintext(e):
    """Read a variable, migrating legacy plaintext secrets to encrypted storage."""
    if not e["is_secret"]:
        return e["value"] or ""
    cipher = e["value_cipher"] if "value_cipher" in e.keys() else None
    box = secrets_for(current_app)
    if cipher:
        return box.decrypt(cipher)
    legacy = e["value"] or ""
    if legacy:
        encrypted = box.encrypt(legacy)
        db = get_db()
        db.execute("UPDATE env_vars SET value = '', value_cipher = ? WHERE id = ? AND value_cipher IS NULL",
                   (encrypted, e["id"]))
        db.commit()
    return legacy


def _env_public(e, reveal=False):
    value = _env_plaintext(e)
    return {"id": e["id"], "key": e["key"], "targets": e["targets"].split(","), "is_secret": bool(e["is_secret"]),
            "value": value if (reveal or not e["is_secret"]) else None, "length": len(value),
            "updated_at": e["updated_at"]}


@bp.get("/projects/<slug>/env")
@login_required
def list_env(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    rows = get_db().execute("SELECT * FROM env_vars WHERE project_id = ? ORDER BY key", (p["id"],)).fetchall()
    return jsonify(env=[_env_public(e) for e in rows])


@bp.get("/projects/<slug>/env/<int:env_id>/reveal")
@login_required
def reveal_env(slug, env_id):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    e = get_db().execute("SELECT * FROM env_vars WHERE id = ? AND project_id = ?", (env_id, p["id"])).fetchone()
    if e is None:
        return err("Variable not found.", 404)
    audit("env.revealed", actor_id=current_user()["id"], target=e["key"], project_id=p["id"])
    return jsonify(env=_env_public(e, reveal=True))


def _clean_targets(t):
    t = [x for x in (t or ["production", "preview"]) if x in ("production", "preview")]
    return ",".join(sorted(set(t))) or "production,preview"


@bp.post("/projects/<slug>/env")
@login_required
def add_env(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    data = request.get_json(silent=True) or {}
    items = data.get("items") if isinstance(data.get("items"), list) else [data]
    db = get_db()
    now = utcnow()
    saved = 0
    for item in items[:200]:
        key = str(item.get("key", "")).strip()
        if not ENV_KEY_RE.match(key):
            return err(f"\"{key or '(empty)'}\" is not a valid variable name. Use letters, digits and underscores.")
        value = str(item.get("value", ""))
        if len(value) > 8000:
            return err(f"{key} is longer than 8,000 characters.")
        is_secret = item.get("is_secret") is not False
        cipher = secrets_for(current_app).encrypt(value) if is_secret else None
        db.execute("INSERT INTO env_vars (project_id, key, value, value_cipher, is_secret, targets, created_at, updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(project_id, key) DO UPDATE SET "
                   "value = excluded.value, value_cipher = excluded.value_cipher, is_secret = excluded.is_secret, "
                   "targets = excluded.targets, updated_at = excluded.updated_at",
                   (p["id"], key, "" if is_secret else value, cipher, 1 if is_secret else 0,
                    _clean_targets(item.get("targets")), now, now))
        saved += 1
    db.commit()
    audit("env.saved", actor_id=current_user()["id"], target=f"{saved} variable(s)", project_id=p["id"])
    return list_env(slug)


@bp.delete("/projects/<slug>/env/<int:env_id>")
@login_required
def delete_env(slug, env_id):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    db = get_db()
    e = db.execute("SELECT key FROM env_vars WHERE id = ? AND project_id = ?", (env_id, p["id"])).fetchone()
    if e is None:
        return err("Variable not found.", 404)
    db.execute("DELETE FROM env_vars WHERE id = ?", (env_id,))
    db.commit()
    audit("env.deleted", actor_id=current_user()["id"], target=e["key"], project_id=p["id"])
    return list_env(slug)


# ------------------------------------------------------------------ domains

def _domain_public(d, project, base):
    return {"id": d["id"], "hostname": d["hostname"], "is_primary": bool(d["is_primary"]),
            "is_system": bool(d["is_system"]), "status": d["status"], "https": bool(d["https"]),
            "created_at": d["created_at"], "verified_at": d["verified_at"],
            "dns": None if d["is_system"] else {
                "cname": {"type": "CNAME", "name": d["hostname"].split(".")[0], "value": f"{project['slug']}.{base}"},
                "txt": {"type": "TXT", "name": f"_codeskys-challenge.{d['hostname']}", "value": f"codeskys-verify={d['verify_token']}"},
            }}


@bp.get("/projects/<slug>/domains")
@login_required
def list_domains(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    db = get_db()
    base = _system_domain(db)
    rows = db.execute("SELECT * FROM domains WHERE project_id = ? ORDER BY is_system DESC, id", (p["id"],)).fetchall()
    return jsonify(domains=[_domain_public(d, p, base) for d in rows])


@bp.post("/projects/<slug>/domains")
@login_required
def add_domain(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    host = str((request.get_json(silent=True) or {}).get("hostname", "")).strip().lower().removeprefix("https://") \
        .removeprefix("http://").strip("/")
    if not HOST_RE.match(host):
        return err("Enter a domain like www.example.com")
    db = get_db()
    if db.execute("SELECT 1 FROM domains WHERE hostname = ?", (host,)).fetchone():
        return err("That domain is already attached to a project.", 409)
    db.execute("INSERT INTO domains (project_id, hostname, status, verify_token, created_at) VALUES (?,?,?,?,?)",
               (p["id"], host, "pending", secrets.token_hex(12), utcnow()))
    db.commit()
    audit("domain.added", actor_id=current_user()["id"], target=host, project_id=p["id"])
    return list_domains(slug)


def _dns(name, rtype):
    try:
        r = requests.get("https://dns.google/resolve", params={"name": name, "type": rtype}, timeout=5)
        return [a.get("data", "").strip('"').rstrip(".") for a in r.json().get("Answer", [])]
    except (requests.RequestException, ValueError):
        return None


@bp.post("/projects/<slug>/domains/<int:dom_id>/verify")
@login_required
def verify_domain(slug, dom_id):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    db = get_db()
    d = db.execute("SELECT * FROM domains WHERE id = ? AND project_id = ?", (dom_id, p["id"])).fetchone()
    if d is None:
        return err("Domain not found.", 404)
    base = _system_domain(db)
    expected_cname = f"{p['slug']}.{base}"
    txt = _dns(f"_codeskys-challenge.{d['hostname']}", "TXT")
    # Records issued before the rebrand used the old _kova-challenge name; keep accepting them.
    legacy_txt = _dns(f"_kova-challenge.{d['hostname']}", "TXT")
    cname = _dns(d["hostname"], "CNAME")
    if txt is None and cname is None:
        return err("Could not reach DNS right now. Try again in a minute.", 503)
    ok = (f"codeskys-verify={d['verify_token']}" in (txt or [])
          or f"kova-verify={d['verify_token']}" in (legacy_txt or [])
          or expected_cname in (cname or []))
    if ok:
        db.execute("UPDATE domains SET status = 'active', verified_at = ? WHERE id = ?", (utcnow(), dom_id))
        db.commit()
        audit("domain.verified", actor_id=current_user()["id"], target=d["hostname"], project_id=p["id"])
    return jsonify(verified=ok, found={"txt": txt or [], "cname": cname or []},
                   domains=list_domains(slug).get_json()["domains"])


@bp.post("/projects/<slug>/domains/<int:dom_id>/primary")
@login_required
def primary_domain(slug, dom_id):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    db = get_db()
    d = db.execute("SELECT * FROM domains WHERE id = ? AND project_id = ?", (dom_id, p["id"])).fetchone()
    if d is None:
        return err("Domain not found.", 404)
    if d["status"] != "active":
        return err("Verify the domain before making it primary.", 409)
    db.execute("UPDATE domains SET is_primary = (id = ?) WHERE project_id = ?", (dom_id, p["id"]))
    db.commit()
    return list_domains(slug)


@bp.delete("/projects/<slug>/domains/<int:dom_id>")
@login_required
def delete_domain(slug, dom_id):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    db = get_db()
    d = db.execute("SELECT * FROM domains WHERE id = ? AND project_id = ?", (dom_id, p["id"])).fetchone()
    if d is None:
        return err("Domain not found.", 404)
    if d["is_system"]:
        return err("The default Codeskys domain can't be removed.", 409)
    db.execute("DELETE FROM domains WHERE id = ?", (dom_id,))
    if d["is_primary"]:
        db.execute("UPDATE domains SET is_primary = 1 WHERE project_id = ? AND is_system = 1", (p["id"],))
    db.commit()
    audit("domain.removed", actor_id=current_user()["id"], target=d["hostname"], project_id=p["id"])
    return list_domains(slug)


# ------------------------------------------------------------------ activity & analytics

@bp.get("/projects/<slug>/activity")
@login_required
def project_activity(slug):
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    rows = get_db().execute("SELECT a.*, u.name AS actor FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id "
                            "WHERE a.project_id = ? ORDER BY a.id DESC LIMIT 100", (p["id"],)).fetchall()
    return jsonify(activity=[{"id": r["id"], "ts": r["ts"], "action": r["action"], "target": r["target"],
                              "detail": r["detail"], "actor": r["actor"] or "System"} for r in rows])


@bp.get("/analytics")
@login_required
def analytics():
    user = current_user()
    db = get_db()
    days = max(7, min(90, request.args.get("days", 30, type=int)))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    where, args = "d.created_at >= ?", [since]
    if user["role"] != "admin":
        where += " AND p.owner_id = ?"
        args.append(user["id"])
    slug = request.args.get("project")
    if slug:
        where += " AND p.slug = ?"
        args.append(slug)
    rows = db.execute(f"SELECT d.*, p.name AS pname, p.slug AS pslug, p.framework AS fw FROM deployments d "
                      f"JOIN projects p ON p.id = d.project_id WHERE {where} ORDER BY d.id", args).fetchall()
    per_project, per_fw, per_trigger = {}, {}, {}
    for d in rows:
        pp = per_project.setdefault(d["pslug"], {"name": d["pname"], "slug": d["pslug"], "total": 0, "ready": 0,
                                                  "failed": 0, "build_ms": 0})
        pp["total"] += 1
        pp[d["status"]] = pp.get(d["status"], 0) + 1
        pp["build_ms"] += d["duration_ms"] or 0
        fwname = FRAMEWORKS.get(d["fw"], FRAMEWORKS["static"])["name"]
        per_fw[fwname] = per_fw.get(fwname, 0) + 1
        per_trigger[d["trigger"]] = per_trigger.get(d["trigger"], 0) + 1
    finished = [d for d in rows if d["duration_ms"] and d["status"] == "ready"]
    durations = sorted(d["duration_ms"] for d in finished)
    p50 = durations[len(durations) // 2] if durations else None
    p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))] if durations else None
    return jsonify(
        days=days, daily=_daily(rows, days), total=len(rows),
        ready=sum(1 for d in rows if d["status"] == "ready"), failed=sum(1 for d in rows if d["status"] == "failed"),
        p50_ms=p50, p95_ms=p95, build_minutes=round(sum(d["duration_ms"] or 0 for d in rows) / 60000, 2),
        projects=sorted(per_project.values(), key=lambda x: -x["total"]),
        frameworks=[{"name": k, "count": v} for k, v in sorted(per_fw.items(), key=lambda x: -x[1])],
        triggers=[{"name": k, "count": v} for k, v in sorted(per_trigger.items(), key=lambda x: -x[1])],
        durations=[{"id": d["id"], "project": d["pslug"], "ms": d["duration_ms"], "at": d["created_at"]}
                   for d in finished[-40:]],
    )
