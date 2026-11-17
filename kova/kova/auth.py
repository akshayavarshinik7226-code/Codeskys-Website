import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request, session

from . import ensure_csrf
from .db import audit, get_db, utcnow
from .ratelimit import rate_limit
from . import browsers
from .security import DUMMY_HASH, hash_password, verify_password
from .users import ValidationError, check_password, create_user, normalise_email, public_user

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def current_user():
    if "kova_user" in g:
        return g.kova_user
    uid = g.get("token_uid") or session.get("uid")
    if not uid:
        return None
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (uid,)).fetchone()
    # A paid plan ends at plan_expires_at (NULL = administrator-set or lifetime-by-admin, never expires).
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

    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    is_admin = user is not None and user["role"] == "admin"
    if is_admin:
        # An administrator is never locked out (otherwise anyone could lock the owner out by typing wrong
        # passwords). Guessing is still capped at a very high number per hour.
        if rate_limit(db, f"login:admin:{user['id']}", limit=current_app.config["ADMIN_LOGIN_FAILS_PER_HOUR"], per_seconds=3600):
            audit("login.ratelimited", actor_id=user["id"], ip=ip, detail="admin ceiling")
            return jsonify(error="Too many sign-in attempts for this account. Try again in a few minutes."), 429
    # Everyone else is rate limited per source address as well as per account, so spraying many
    # different emails from one host is throttled too.
    elif rate_limit(db, f"login:ip:{ip}", limit=20, per_seconds=300):
        audit("login.ratelimited", target=email, ip=ip)
        return jsonify(error="Too many sign-in attempts from this address. Try again shortly."), 429

    if user is None:
        verify_password(password, DUMMY_HASH)  # equalise timing
        audit("login.failed", target=email, ip=ip, detail="unknown email")
        return generic

    now = datetime.now(timezone.utc)
    if not is_admin and user["locked_until"] and datetime.fromisoformat(user["locked_until"]) > now:
        audit("login.blocked", actor_id=user["id"], ip=ip, detail="account locked")
        minutes = current_app.config["LOGIN_LOCK_MINUTES"]
        return jsonify(error=f"Too many failed attempts. Try again in {minutes} minutes."), 429

    if not user["is_active"] or not verify_password(password, user["password_hash"]):
        failures = user["failed_logins"] + 1
        locked = None
        if failures >= current_app.config["LOGIN_MAX_FAILURES"] and not is_admin:
            locked = (now + timedelta(minutes=current_app.config["LOGIN_LOCK_MINUTES"])).isoformat(timespec="seconds")
            failures = 0
        db.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (failures, locked, user["id"]))
        db.commit()
        audit("login.failed", actor_id=user["id"], ip=ip, detail="locked" if locked else "bad password")
        return generic

    blocked = browsers.check_login(db, user)       # one account per browser (administrators are exempt)
    if blocked:
        audit("login.browser_linked", actor_id=user["id"], ip=ip)
        return jsonify(error=blocked), 403
    db.execute("UPDATE users SET failed_logins=0, locked_until=NULL, last_login_at=? WHERE id=?", (utcnow(), user["id"]))
    db.commit()
    session.clear()  # prevent session fixation
    g.pop("kova_user", None)
    session["uid"] = user["id"]
    session.permanent = True                      # stay signed in (30 days by default), not just until the browser closes
    csrf = ensure_csrf()
    audit("login.ok", actor_id=user["id"], ip=ip)
    return jsonify(user=public_user(user), csrf=csrf)


# Sign-up is two steps: details + password, then the 6-digit code sent to the email address.
# The account does not exist until the code is entered, so a made-up or someone else's
# address can never become an account.
OTP_RESEND_SECONDS = 30
OTP_MAX_SENDS = 5
OTP_MAX_ATTEMPTS = 5


def _otp_hash(email: str, code: str) -> str:
    key = str(current_app.config["SECRET_KEY"]).encode()
    return hmac.new(key, f"{email}|{code}".encode(), hashlib.sha256).hexdigest()


def _start_session(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    session.clear()  # prevent session fixation
    g.pop("kova_user", None)
    session["uid"] = user_id
    session.permanent = True
    browsers.bind(db, user)                       # this browser now belongs to the new account
    return user, ensure_csrf()


def _send_code(db, email: str, name: str, password_hash: str, resend: bool = False):
    """Store a fresh code for ``email`` and email it. Returns (response, status) on a problem."""
    from . import mailer
    now = datetime.now(timezone.utc)
    pending = db.execute("SELECT * FROM pending_signups WHERE email = ?", (email,)).fetchone()
    sends = 1
    if pending is not None:
        expired = datetime.fromisoformat(pending["expires_at"]) <= now
        if not expired:
            wait = OTP_RESEND_SECONDS - (now - datetime.fromisoformat(pending["last_sent_at"])).total_seconds()
            if wait > 0:
                return jsonify(error=f"Please wait {int(wait) + 1} seconds before asking for another code.",
                               retry_after=int(wait) + 1), 429
            if pending["sends"] >= OTP_MAX_SENDS:
                return jsonify(error="Too many codes were requested for this email. Try again in a few minutes."), 429
            sends = pending["sends"] + 1
    code = f"{secrets.randbelow(1_000_000):06d}"
    minutes = int(current_app.config["OTP_MINUTES"])
    expires = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO pending_signups (email, name, password_hash, code_hash, attempts, sends, expires_at, "
        "last_sent_at, created_at) VALUES (?,?,?,?,0,?,?,?,?) ON CONFLICT(email) DO UPDATE SET name = excluded.name, "
        "password_hash = excluded.password_hash, code_hash = excluded.code_hash, attempts = 0, sends = excluded.sends, "
        "expires_at = excluded.expires_at, last_sent_at = excluded.last_sent_at",
        (email, name, password_hash, _otp_hash(email, code), sends, expires, utcnow(), utcnow()))
    db.commit()
    subject, text, html = mailer.otp_email(current_app.config, code, minutes)
    try:
        mailer.send_mail(current_app, email, subject, text, html)
    except mailer.MailNotConfigured:
        db.execute("DELETE FROM pending_signups WHERE email = ?", (email,))
        db.commit()
        return jsonify(error="Email verification is not set up on this server yet. Please contact the Codeskys team."), 503
    except mailer.MailFailed:
        db.execute("DELETE FROM pending_signups WHERE email = ?", (email,))
        db.commit()
        return jsonify(error="We couldn't send the verification email. Check the address and try again."), 502
    return None


@bp.post("/register")
def register():
    """Step 1 of sign-up: check the details and email a verification code.

    Public registration can never grant admin. With ``KOVA_EMAIL_OTP=0`` the account is created
    straight away (kept only so an owner can switch verification off temporarily).
    """
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    raw_email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    db = get_db()
    ip = request.remote_addr
    if rate_limit(db, f"register:ip:{ip}", limit=10, per_seconds=3600):
        audit("register.ratelimited", target=raw_email, ip=ip)
        return jsonify(error="Too many account creation attempts. Please try again later."), 429
    try:
        email = normalise_email(raw_email)
        if not name:
            raise ValidationError("Enter your name.")
        if len(name) > 80:
            raise ValidationError("Your name is too long.")
        check_password(password, email)
    except ValidationError as exc:
        return jsonify(error=str(exc)), 400
    if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        return jsonify(error="An account with this email already exists. Log In instead."), 409
    linked = browsers.check_signup(db)             # one account per browser
    if linked:
        return jsonify(error=linked), 403
    if not current_app.config.get("EMAIL_OTP_REQUIRED", True):
        try:
            user_id = create_user(db, email, name, password, "student")
        except ValidationError as exc:
            return jsonify(error=str(exc)), 400
        user, csrf = _start_session(user_id)
        audit("register.ok", actor_id=user_id, target=email, ip=ip)
        return jsonify(user=public_user(user), csrf=csrf), 201
    if rate_limit(db, f"register:email:{email}", limit=6, per_seconds=3600):
        return jsonify(error="Too many attempts for this email. Please try again later."), 429
    problem = _send_code(db, email, name, hash_password(password))
    if problem is not None:
        return problem
    audit("register.code_sent", target=email, ip=ip)
    minutes = int(current_app.config["OTP_MINUTES"])
    return jsonify(verify=True, email=email, expires_in=minutes * 60, resend_after=OTP_RESEND_SECONDS), 202


@bp.post("/register/resend")
def register_resend():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    db = get_db()
    if rate_limit(db, f"register:resend:{request.remote_addr}", limit=20, per_seconds=3600):
        return jsonify(error="Too many requests. Please try again later."), 429
    pending = db.execute("SELECT * FROM pending_signups WHERE email = ?", (email,)).fetchone()
    if pending is None:
        return jsonify(error="That sign-up has expired. Start again."), 404
    problem = _send_code(db, email, pending["name"], pending["password_hash"], resend=True)
    if problem is not None:
        return problem
    return jsonify(ok=True, expires_in=int(current_app.config["OTP_MINUTES"]) * 60, resend_after=OTP_RESEND_SECONDS)


@bp.post("/register/verify")
def register_verify():
    """Step 2 of sign-up: the code from the email creates the account and signs the person in."""
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    code = "".join(ch for ch in str(data.get("code", "")) if ch.isdigit())
    db = get_db()
    ip = request.remote_addr
    if rate_limit(db, f"register:verify:{ip}", limit=30, per_seconds=3600):
        return jsonify(error="Too many attempts. Please try again later."), 429
    pending = db.execute("SELECT * FROM pending_signups WHERE email = ?", (email,)).fetchone()
    if pending is None:
        return jsonify(error="That sign-up has expired. Start again."), 404
    if datetime.fromisoformat(pending["expires_at"]) <= datetime.now(timezone.utc):
        db.execute("DELETE FROM pending_signups WHERE id = ?", (pending["id"],))
        db.commit()
        return jsonify(error="That code has expired. Start again to get a new one."), 410
    linked = browsers.check_signup(db)
    if linked:
        return jsonify(error=linked), 403
    if len(code) != 6 or not hmac.compare_digest(_otp_hash(email, code), pending["code_hash"]):
        attempts = pending["attempts"] + 1
        if attempts >= OTP_MAX_ATTEMPTS:
            db.execute("DELETE FROM pending_signups WHERE id = ?", (pending["id"],))
            db.commit()
            audit("register.code_locked", target=email, ip=ip)
            return jsonify(error="Too many wrong codes. Start again to get a new one."), 429
        db.execute("UPDATE pending_signups SET attempts = ? WHERE id = ?", (attempts, pending["id"]))
        db.commit()
        left = OTP_MAX_ATTEMPTS - attempts
        return jsonify(error=f"That code is not right. {left} {'try' if left == 1 else 'tries'} left."), 400
    try:
        user_id = create_user(db, email, pending["name"], "", "student", password_hash=pending["password_hash"])
    except ValidationError as exc:
        db.execute("DELETE FROM pending_signups WHERE id = ?", (pending["id"],))
        db.commit()
        return jsonify(error=str(exc)), 409
    db.execute("DELETE FROM pending_signups WHERE id = ?", (pending["id"],))
    db.commit()
    user, csrf = _start_session(user_id)
    audit("register.ok", actor_id=user_id, target=email, ip=ip, detail="email verified")
    return jsonify(user=public_user(user), csrf=csrf), 201


@bp.post("/logout")
def logout():
    user = current_user()
    if user:
        audit("logout", actor_id=user["id"], ip=request.remote_addr)
    session.clear()
    g.pop("kova_user", None)
    return jsonify(ok=True, csrf=ensure_csrf())
