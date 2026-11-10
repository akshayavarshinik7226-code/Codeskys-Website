from urllib.parse import urlparse

from flask import Blueprint, current_app, jsonify

from .auth import current_user, login_required
from .db import get_db, utcnow

bp = Blueprint("status", __name__, url_prefix="/api")


@bp.get("/health")
def health():
    """Unauthenticated liveness probe (no sensitive data)."""
    try:
        get_db().execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:  # pragma: no cover - defensive
        db_ok = False
    code = 200 if db_ok else 503
    return jsonify(status="ok" if db_ok else "degraded", database=db_ok,
                   version=current_app.config["VERSION"], time=utcnow()), code


@bp.get("/platform/status")
@login_required
def platform_status():
    """Real status only. Coolify is reported as not_configured until Phase 5 adds the live check."""
    from .engine import active_provider, coolify_settings
    cs = coolify_settings(get_db(), current_app.config)
    url, token = cs["url"], cs["token"]
    configured = bool(url and token)
    coolify = {"configured": configured, "state": "unchecked" if configured else "not_configured",
               "host": urlparse(url).netloc if url else None}
    row = get_db().execute("SELECT COUNT(*) AS n FROM users").fetchone()
    is_admin = current_user()["role"] == "admin"
    return jsonify(
        platform=current_app.config["PLATFORM_NAME"],
        version=current_app.config["VERSION"],
        database={"ok": True},
        coolify=coolify,
        provider=active_provider(get_db(), current_app.config),
        users=row["n"] if is_admin else None,
    )
