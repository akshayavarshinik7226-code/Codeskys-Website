"""Push webhooks from GitHub and GitLab.

Every request must prove it came from the provider before anything happens:

* **GitHub** signs the raw body with HMAC-SHA256 (``X-Hub-Signature-256``). The
  signature is compared in constant time against the project's own secret.
* **GitLab** sends a shared secret in ``X-Gitlab-Token``, also compared in
  constant time.

Each project gets its own secret, so one leaked secret cannot trigger another
project. Deliveries are recorded by the provider's delivery id and replayed
deliveries are ignored, which also stops a captured-and-replayed request from
queueing a second build.
"""
import hashlib
import hmac
import json

from flask import Blueprint, current_app, jsonify, request

from .db import audit, get_db, utcnow
from .ratelimit import rate_limit

bp = Blueprint("webhooks", __name__, url_prefix="/api/hooks")
MAX_BODY = 2 * 1024 * 1024  # GitHub push payloads are well under this


def _reply(ok, message, code=200, **extra):
    return jsonify(ok=ok, message=message, **extra), code


def _project_by_secret_id(conn, hook_id):
    return conn.execute("SELECT * FROM projects WHERE hook_token = ?", (hook_id,)).fetchone()


def _record(conn, project_id, provider, delivery_id, event, sha, result):
    try:
        conn.execute(
            "INSERT INTO webhook_deliveries (project_id, provider, delivery_id, event, commit_sha, result, "
            "received_at) VALUES (?,?,?,?,?,?,?)",
            (project_id, provider, delivery_id, event, sha or "", result, utcnow()))
        conn.commit()
        return True
    except Exception as exc:
        if "UNIQUE" in str(exc):
            conn.rollback()
            return False
        raise


def _already_delivered(conn, provider, delivery_id):
    if not delivery_id:
        return False
    return conn.execute("SELECT 1 FROM webhook_deliveries WHERE provider = ? AND delivery_id = ?",
                        (provider, delivery_id)).fetchone() is not None


def _verify_github(secret: str, raw: bytes, header: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header.split("=", 1)[1].strip())


def _trigger(conn, project, branch, sha, message, provider, delivery_id):
    """Queue a deployment for a verified push. Returns (queued, reason)."""
    from .projects import trigger_deployment  # imported here to avoid a circular import

    if project["is_paused"]:
        return False, "Project is paused."
    if not project["auto_deploy"]:
        return False, "Automatic deployments are turned off for this project."
    if branch and project["branch"] and branch != project["branch"]:
        return False, f"Push was to '{branch}', which is not this project's deploy branch ('{project['branch']}')."
    # Duplicate guard: the same commit already building or live needs no second build.
    existing = conn.execute(
        "SELECT id, status FROM deployments WHERE project_id = ? AND commit_sha = ? AND target = 'production' "
        "AND status IN ('queued','building') ORDER BY id DESC LIMIT 1", (project["id"], sha)).fetchone()
    if existing:
        return False, f"Commit {sha[:7]} is already building (deployment #{existing['id']})."
    dep_id = trigger_deployment(conn, project, branch=branch or project["branch"], trigger=f"{provider}-push",
                                commit_sha=sha, commit_message=message)
    audit("deploy.webhook", target=f"#{dep_id}", detail=f"{provider} {delivery_id}"[:200],
          ip=request.remote_addr, project_id=project["id"])
    return True, dep_id


@bp.post("/github/<hook_id>")
def github(hook_id):
    conn = get_db()
    if rate_limit(conn, f"hook:{hook_id}", limit=60, per_seconds=60):
        return _reply(False, "Too many webhook deliveries; slow down.", 429)
    raw = request.get_data(cache=False)
    if len(raw) > MAX_BODY:
        return _reply(False, "Payload too large.", 413)
    project = _project_by_secret_id(conn, hook_id)
    # Unknown hook and bad signature return the same shape, so the endpoint cannot
    # be used to discover which hook ids exist.
    if project is None or not project["webhook_secret"]:
        return _reply(False, "Signature verification failed.", 401)
    if not _verify_github(project["webhook_secret"], raw, request.headers.get("X-Hub-Signature-256", "")):
        audit("webhook.rejected", target="github", detail="bad signature", ip=request.remote_addr,
              project_id=project["id"])
        return _reply(False, "Signature verification failed.", 401)

    event = request.headers.get("X-GitHub-Event", "")
    delivery = request.headers.get("X-GitHub-Delivery", "")
    if event == "ping":
        _record(conn, project["id"], "github", delivery or "ping", event, "", "ping acknowledged")
        return _reply(True, "Webhook configured correctly.")
    if event != "push":
        return _reply(True, f"Ignored '{event}' event.")
    if _already_delivered(conn, "github", delivery):
        return _reply(True, "Duplicate delivery ignored.")

    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _reply(False, "Payload was not valid JSON.", 400)
    ref = body.get("ref") or ""
    branch = ref.split("refs/heads/", 1)[1] if ref.startswith("refs/heads/") else ""
    if body.get("deleted"):
        _record(conn, project["id"], "github", delivery, event, "", "branch deleted; ignored")
        return _reply(True, "Branch deletion ignored.")
    head = body.get("head_commit") or {}
    sha = body.get("after") or head.get("id") or ""
    message = (head.get("message") or "").splitlines()[0][:200] if head else ""
    if not _record(conn, project["id"], "github", delivery or sha, event, sha, "accepted"):
        return _reply(True, "Duplicate delivery ignored.")
    queued, detail = _trigger(conn, project, branch, sha, message, "github", delivery)
    conn.execute("UPDATE webhook_deliveries SET result = ? WHERE provider = 'github' AND delivery_id = ?",
                 (f"deployment {detail}" if queued else str(detail), delivery or sha))
    conn.commit()
    return _reply(True, "Deployment queued." if queued else str(detail),
                  **({"deployment_id": detail} if queued else {}))


@bp.post("/gitlab/<hook_id>")
def gitlab(hook_id):
    conn = get_db()
    if rate_limit(conn, f"hook:{hook_id}", limit=60, per_seconds=60):
        return _reply(False, "Too many webhook deliveries; slow down.", 429)
    raw = request.get_data(cache=False)
    if len(raw) > MAX_BODY:
        return _reply(False, "Payload too large.", 413)
    project = _project_by_secret_id(conn, hook_id)
    supplied = request.headers.get("X-Gitlab-Token", "")
    if project is None or not project["webhook_secret"] or not supplied or \
            not hmac.compare_digest(project["webhook_secret"], supplied):
        if project is not None:
            audit("webhook.rejected", target="gitlab", detail="bad token", ip=request.remote_addr,
                  project_id=project["id"])
        return _reply(False, "Token verification failed.", 401)

    event = request.headers.get("X-Gitlab-Event", "")
    delivery = request.headers.get("X-Gitlab-Event-UUID", "")
    if event not in ("Push Hook", ""):
        return _reply(True, f"Ignored '{event}' event.")
    if _already_delivered(conn, "gitlab", delivery):
        return _reply(True, "Duplicate delivery ignored.")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _reply(False, "Payload was not valid JSON.", 400)
    ref = body.get("ref") or ""
    branch = ref.split("refs/heads/", 1)[1] if ref.startswith("refs/heads/") else ""
    sha = body.get("after") or body.get("checkout_sha") or ""
    if sha and set(sha) == {"0"}:
        _record(conn, project["id"], "gitlab", delivery or "del", event, "", "branch deleted; ignored")
        return _reply(True, "Branch deletion ignored.")
    commits = body.get("commits") or []
    message = (commits[-1].get("message", "").splitlines()[0][:200]) if commits else ""
    if not _record(conn, project["id"], "gitlab", delivery or sha, event, sha, "accepted"):
        return _reply(True, "Duplicate delivery ignored.")
    queued, detail = _trigger(conn, project, branch, sha, message, "gitlab", delivery)
    conn.execute("UPDATE webhook_deliveries SET result = ? WHERE provider = 'gitlab' AND delivery_id = ?",
                 (f"deployment {detail}" if queued else str(detail), delivery or sha))
    conn.commit()
    return _reply(True, "Deployment queued." if queued else str(detail),
                  **({"deployment_id": detail} if queued else {}))


@bp.post("/deploy/<token>")
def generic(token):
    """Plain CI hook: no provider signature, so the secret URL is the only credential."""
    conn = get_db()
    if rate_limit(conn, f"hook:{token}", limit=30, per_seconds=60):
        return _reply(False, "Too many requests.", 429)
    from .projects import trigger_deployment
    from .gitinfo import GitError

    project = _project_by_secret_id(conn, token)
    if project is None or len(token) < 20:
        return _reply(False, "Unknown deploy hook.", 404)
    if not project["auto_deploy"] and request.args.get("force") != "1":
        return _reply(False, "Auto-deploy is disabled for this project.", 409)
    try:
        dep_id = trigger_deployment(conn, project, trigger="hook",
                                    branch=request.args.get("branch") or None)
    except GitError as exc:
        return _reply(False, str(exc), 409)
    audit("deploy.hook", target=f"#{dep_id}", ip=request.remote_addr, project_id=project["id"])
    return _reply(True, "Deployment queued.", 201, deployment_id=dep_id)
