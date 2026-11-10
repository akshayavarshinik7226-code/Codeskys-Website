from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request, session

from . import ensure_csrf
from .db import audit, get_db, utcnow
from .ratelimit import rate_limit
from .security import DUMMY_HASH, verify_password
from .users import ValidationError, create_user, public_user

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def current_user():
    if "kova_user" in g:
        return g.kova_user
    uid = g.get("token_uid") or session.get("uid")
    if not uid:
        return None
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (uid,)).fetchone()
    # A paid plan ends at plan_expires_at (NULL = administrator-set, never expires).
    if row is not None and "plan_expires_at" in row.keys() and row["plan_expires_at"]             and row["plan_expires_at"] <= utcnow():
        db.execute("UPDATE users SET plan = 'hobby', plan_expires_at = NULL WHERE id = ? AND plan_expires_at = ?",
                   (row["id"], row["plan_expires_at"]))
        db.commit()
        audit("billing.plan_expired", actor_id=row["id"], detail=row["plan"])
        row = db.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (uid,)).fetchone()
    g.kova_user = row
    return row


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if current_user() is None:
            return jsonify(error="Sign in to continue."), 401
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        user = current_user()
        if user is None:
            return jsonify(error="Sign in to continue."), 401
        if user["role"] != "admin":
            return jsonify(error="Only administrators can do this."), 403
        return fn(*a, **kw)
    return wrapper


@bp.get("/me")
def me():
    csrf = ensure_csrf()
    user = current_user()
    return jsonify(user=public_user(user) if user else None, csrf=csrf)


@bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    db = get_db()
    ip = request.remote_addr
    generic = jsonify(error="Email or password is incorrect."), 401

    # Rate limited per source address as well as per account, so spraying many
    # different emails from one host is throttled too.
    if rate_limit(db, f"login:ip:{ip}", limit=20, per_seconds=300):
        audit("login.ratelimited", target=email, ip=ip)
        return jsonify(error="Too many sign-in attempts from this address. Try again shortly."), 429

    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user is None:
        verify_password(password, DUMMY_HASH)  # equalise timing
        audit("login.failed", target=email, ip=ip, detail="unknown email")
        return generic

    now = datetime.now(timezone.utc)
    if user["locked_until"] and datetime.fromisoformat(user["locked_until"]) > now:
        audit("login.blocked", actor_id=user["id"], ip=ip, detail="account locked")
        minutes = current_app.config["LOGIN_LOCK_MINUTES"]
        return jsonify(error=f"Too many failed attempts. Try again in {minutes} minutes."), 429

    if not user["is_active"] or not verify_password(password, user["password_hash"]):
        failures = user["failed_logins"] + 1
        locked = None
        if failures >= current_app.config["LOGIN_MAX_FAILURES"]:
            locked = (now + timedelta(minutes=current_app.config["LOGIN_LOCK_MINUTES"])).isoformat(timespec="seconds")
            failures = 0
        db.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (failures, locked, user["id"]))
        db.commit()
        audit("login.failed", actor_id=user["id"], ip=ip, detail="locked" if locked else "bad password")
        return generic

    db.execute("UPDATE users SET failed_logins=0, locked_until=NULL, last_login_at=? WHERE id=?", (utcnow(), user["id"]))
    db.commit()
    session.clear()  # prevent session fixation
    g.pop("kova_user", None)
    session["uid"] = user["id"]
    csrf = ensure_csrf()
    audit("login.ok", actor_id=user["id"], ip=ip)
    return jsonify(user=public_user(user), csrf=csrf)


@bp.post("/register")
def register():
    """Create a normal student account; public registration can never grant admin."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    db = get_db()
    ip = request.remote_addr
    if rate_limit(db, f"register:ip:{ip}", limit=5, per_seconds=3600):
        audit("register.ratelimited", target=email, ip=ip)
        return jsonify(error="Too many account creation attempts. Please try again later."), 429
    try:
        user_id = create_user(db, email, name, password, "student")
    except ValidationError as exc:
        return jsonify(error=str(exc)), 400
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    session.clear()
    g.pop("kova_user", None)
    session["uid"] = user_id
    csrf = ensure_csrf()
    audit("register.ok", actor_id=user_id, target=email, ip=ip)
    return jsonify(user=public_user(user), csrf=csrf), 201


@bp.post("/logout")
def logout():
    user = current_user()
    if user:
        audit("logout", actor_id=user["id"], ip=request.remote_addr)
    session.clear()
    g.pop("kova_user", None)
    return jsonify(ok=True, csrf=ensure_csrf())
