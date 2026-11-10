"""Profile, password, personal API tokens, notifications and activity feed."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from .auth import current_user, login_required
from .db import audit, get_db, utcnow
from .security import hash_password, verify_password
from .users import MIN_PASSWORD, public_user

bp = Blueprint("account", __name__, url_prefix="/api")


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@bp.patch("/account")
@login_required
def update_profile():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:80]
    if not name:
        return jsonify(error="Enter your name."), 400
    db = get_db()
    user = current_user()
    db.execute("UPDATE users SET name = ? WHERE id = ?", (name, user["id"]))
    db.commit()
    audit("account.updated", actor_id=user["id"])
    return jsonify(user=public_user(db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()))


@bp.post("/account/password")
@login_required
def change_password():
    data = request.get_json(silent=True) or {}
    user = current_user()
    if not verify_password(str(data.get("current", "")), user["password_hash"]):
        return jsonify(error="Your current password is incorrect."), 400
    new = str(data.get("new", ""))
    if len(new) < MIN_PASSWORD:
        return jsonify(error=f"New password must be at least {MIN_PASSWORD} characters."), 400
    db = get_db()
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new), user["id"]))
    db.commit()
    audit("account.password_changed", actor_id=user["id"], ip=request.remote_addr)
    return jsonify(ok=True)


# ---------------------------------------------------------------- tokens

@bp.get("/tokens")
@login_required
def list_tokens():
    rows = get_db().execute("SELECT * FROM api_tokens WHERE user_id = ? ORDER BY id DESC", (current_user()["id"],)).fetchall()
    return jsonify(tokens=[{"id": r["id"], "name": r["name"], "prefix": r["prefix"], "created_at": r["created_at"],
                            "last_used_at": r["last_used_at"], "expires_at": r["expires_at"]} for r in rows])


@bp.post("/tokens")
@login_required
def create_token():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:60]
    if not name:
        return jsonify(error="Name the token so you can recognise it later."), 400
    days = data.get("expires_days")
    expires = None
    if days:
        try:
            expires = (datetime.now(timezone.utc) + timedelta(days=int(days))).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            return jsonify(error="Expiry must be a number of days."), 400
    raw = "kova_" + secrets.token_urlsafe(32)
    db = get_db()
    user = current_user()
    db.execute("INSERT INTO api_tokens (user_id, name, prefix, token_hash, created_at, expires_at) VALUES (?,?,?,?,?,?)",
               (user["id"], name, raw[:12], token_hash(raw), utcnow(), expires))
    db.commit()
    audit("token.created", actor_id=user["id"], target=name)
    return jsonify(token=raw, tokens=list_tokens().get_json()["tokens"]), 201


@bp.delete("/tokens/<int:tid>")
@login_required
def revoke_token(tid):
    db = get_db()
    user = current_user()
    db.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (tid, user["id"]))
    db.commit()
    audit("token.revoked", actor_id=user["id"], target=str(tid))
    return list_tokens()


# ---------------------------------------------------------------- notifications

@bp.get("/notifications")
@login_required
def notifications():
    db = get_db()
    uid = current_user()["id"]
    rows = db.execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 100", (uid,)).fetchall()
    unread = db.execute("SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND is_read = 0", (uid,)).fetchone()["n"]
    return jsonify(unread=unread, notifications=[{k: r[k] for k in ("id", "kind", "title", "body", "link", "created_at")}
                                                 | {"is_read": bool(r["is_read"])} for r in rows])


@bp.post("/notifications/read")
@login_required
def mark_read():
    data = request.get_json(silent=True) or {}
    db = get_db()
    uid = current_user()["id"]
    if data.get("id"):
        db.execute("UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?", (data["id"], uid))
    else:
        db.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (uid,))
    db.commit()
    return notifications()


# ---------------------------------------------------------------- activity

@bp.get("/activity")
@login_required
def activity():
    user = current_user()
    db = get_db()
    if user["role"] == "admin":
        rows = db.execute("SELECT a.*, u.name AS actor, p.name AS pname, p.slug AS pslug FROM audit_log a "
                          "LEFT JOIN users u ON u.id = a.actor_id LEFT JOIN projects p ON p.id = a.project_id "
                          "ORDER BY a.id DESC LIMIT 200").fetchall()
    else:
        rows = db.execute("SELECT a.*, u.name AS actor, p.name AS pname, p.slug AS pslug FROM audit_log a "
                          "LEFT JOIN users u ON u.id = a.actor_id LEFT JOIN projects p ON p.id = a.project_id "
                          "WHERE a.actor_id = ? OR p.owner_id = ? ORDER BY a.id DESC LIMIT 200",
                          (user["id"], user["id"])).fetchall()
    return jsonify(activity=[{"id": r["id"], "ts": r["ts"], "action": r["action"], "target": r["target"],
                              "detail": r["detail"], "ip": r["ip"] if user["role"] == "admin" else None,
                              "actor": r["actor"] or "System", "project": r["pname"], "project_slug": r["pslug"]}
                             for r in rows])
