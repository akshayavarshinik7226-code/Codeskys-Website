import hmac
import os
import secrets

from flask import Flask, g, jsonify, request, session

from . import db as dbmod
from .config import build_config, load_dotenv
from .users import ValidationError, create_user

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def create_app(test_config: dict | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__)
    app.config.update(build_config())
    if test_config:
        app.config.update(test_config)

    dbmod.migrate(app.config["DATABASE"])
    app.teardown_appcontext(dbmod.close_db)
    _bootstrap_admin(app)

    from .auth import bp as auth_bp
    from .pages import bp as pages_bp
    from .status import bp as status_bp
    from .projects import bp as projects_bp
    from .account import bp as account_bp
    from .admin import bp as admin_bp
    from .sites import bp as sites_bp
    from .providers import bp as providers_bp
    from .webhooks import bp as webhooks_bp
    from .forms import bp as forms_bp
    from .billing import bp as billing_bp

    for blueprint in (auth_bp, status_bp, pages_bp, projects_bp, account_bp, admin_bp, sites_bp,
                      providers_bp, webhooks_bp, forms_bp, billing_bp):
        app.register_blueprint(blueprint)

    # Job handlers register themselves on import; the worker needs them loaded.
    from . import engine  # noqa: F401

    _start_inline_worker(app)

    @app.before_request
    def bearer_token():
        header = request.headers.get("Authorization", "")
        if not (request.path.startswith("/api/") and header.startswith("Bearer kova_")):
            return None
        from .account import token_hash
        from .db import get_db, utcnow
        db = get_db()
        row = db.execute("SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash(header[7:].strip()),)).fetchone()
        if row is None or (row["expires_at"] and row["expires_at"] < utcnow()):
            return jsonify(error="Invalid or expired API token."), 401
        db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (utcnow(), row["id"]))
        db.commit()
        g.token_uid = row["user_id"]
        return None

    @app.before_request
    def csrf_protect():
        # Webhooks carry their own proof: a provider HMAC signature or a secret
        # URL token. They cannot send a session CSRF header, so they are exempt
        # here and authenticated in kova.webhooks instead.
        if (g.get("token_uid") or request.path.startswith("/api/hooks/")
                or request.path.startswith("/api/public/forms/")):
            return None
        if request.path.startswith("/api/") and request.method not in SAFE_METHODS:
            sent = request.headers.get("X-CSRF-Token", "")
            expected = session.get("csrf", "")
            if not expected or not hmac.compare_digest(sent, expected):
                return jsonify(error="Your session expired. Reload the page and try again."), 403

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        # Razorpay Checkout (script + iframe) is allowed only on the signed-in app shell
        # and only when billing is configured.
        rzp = ""
        if request.path == "/":
            from .billing import get_config as billing_config
            bc = billing_config()
            if bc.enabled and bc.checkout_open and session.get("uid"):
                rzp = " https://checkout.razorpay.com"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:" + (" https:" if rzp else "") + "; "
            "style-src 'self' 'unsafe-inline'; font-src 'self'; "
            "script-src 'self'" + rzp + "; "
            "connect-src 'self'" + (" https://api.razorpay.com https://lumberjack.razorpay.com" if rzp else "") + "; "
            + ("frame-src https://api.razorpay.com https://checkout.razorpay.com; " if rzp else "")
            + "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found."), 404
        return "Not found", 404

    @app.errorhandler(405)
    def not_allowed(_e):
        return jsonify(error="Method not allowed."), 405

    return app


def _start_inline_worker(app):
    """Run the queue worker in-process unless a dedicated worker is used.

    The queue itself is in the database, so this is only about who turns the
    crank. Tests and `manage.py worker` opt out and drive it explicitly.
    """
    if not app.config.get("INLINE_WORKER", True) or app.config.get("TESTING"):
        return
    # Flask's reloader runs the module twice; only the child process should work.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    from .jobs import Worker

    worker = Worker(app.config["DATABASE"], app.config)
    app.config["_WORKER"] = worker
    worker.start_thread()
    app.logger.info("Codeskys inline queue worker started (%s)", worker.id)


def ensure_csrf() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def _bootstrap_admin(app: Flask):
    """Create the first admin from env vars only when no users exist."""
    email = os.environ.get("KOVA_ADMIN_EMAIL", "").strip()
    password = os.environ.get("KOVA_ADMIN_PASSWORD", "")
    if not email or not password:
        return
    conn = dbmod.connect(app.config["DATABASE"])
    try:
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            return
        try:
            create_user(conn, email, os.environ.get("KOVA_ADMIN_NAME", "Administrator"), password, "admin")
            app.logger.info("Bootstrap admin created for %s", email)
        except ValidationError as exc:
            app.logger.error("Bootstrap admin NOT created: %s", exc)
    finally:
        conn.close()
