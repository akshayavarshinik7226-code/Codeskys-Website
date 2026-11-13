"""Certificate approval for Caddy's on-demand TLS (``on_demand_tls { ask ... }``).

Caddy calls ``GET /internal/tls-ask?domain=<host>`` before it requests a Let's
Encrypt certificate for an app hostname. Answering 200 for anything would let
anyone point a DNS name at the server and make Caddy request certificates for it
(rate limits, abuse), so this approves exactly one thing: ``<slug>.<base domain>``
for a project whose current deployment is live (ready, not expired) on Coolify.

Defence in depth:
* Caddy blocks ``/internal*`` on the public site (``Caddyfile.https``).
* Requests that came through a reverse proxy (``X-Forwarded-For`` present) are
  refused: Caddy's own ``ask`` request is direct and has no such header.
* The answer is a bare status code; it reveals nothing beyond what the public
  app URL already shows.

``/internal/app-gate`` is the matching request gate (Caddy ``forward_auth`` in
front of every app request): 200 lets the request through to the app; otherwise
the visitor gets the Codeskys "Deployment expired" / "not available" page. The
24-hour limit is therefore enforced at the edge at the exact expiry time, even
before the expiry job runs and even when deleting the Coolify app failed.
"""
import re
from pathlib import Path

from flask import Blueprint, current_app, render_template, request
from markupsafe import Markup

from .db import get_db, get_setting, utcnow

bp = Blueprint("tlsask", __name__)

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _base_domain(conn) -> str:
    base = current_app.config.get("COOLIFY_BASE_DOMAIN") or get_setting(conn, "coolify_base_domain", "") or ""
    return base.strip().lower().strip(".")


def _slug(domain: str, base: str):
    """The project slug for ``<slug>.<base>``, or None for anything else."""
    domain = (domain or "").strip().lower().rstrip(".")
    if not base or not domain or len(domain) > 253 or not domain.endswith("." + base):
        return None
    label = domain[: -(len(base) + 1)]
    return label if _LABEL.match(label) else None   # exactly one DNS label: no nested subdomains


def approved(conn, domain: str, base: str) -> bool:
    label = _slug(domain, base)
    if label is None:
        return False
    row = conn.execute(
        "SELECT d.status, d.expires_at FROM projects p JOIN deployments d ON d.id = p.current_deployment_id "
        "WHERE p.slug = ? AND p.provider = 'coolify' AND p.provider_ref IS NOT NULL AND p.provider_ref != '' "
        "AND d.provider = 'coolify'", (label,)).fetchone()
    if row is None or row["status"] != "ready":
        return False
    return not row["expires_at"] or row["expires_at"] > utcnow()


@bp.get("/internal/tls-ask")
def tls_ask():
    if request.headers.get("X-Forwarded-For") or request.headers.get("Forwarded"):
        return "", 404                      # came through the public proxy: not Caddy's ask
    conn = get_db()
    return ("", 200) if approved(conn, request.args.get("domain", ""), _base_domain(conn)) else ("", 404)


def _gate_state(conn, domain: str, base: str):
    """("live" | "expired" | "unavailable" | "unknown", project row or None)."""
    label = _slug(domain, base)
    if label is None:
        return "unknown", None
    project = conn.execute("SELECT * FROM projects WHERE slug = ?", (label,)).fetchone()
    if project is None:
        return "unknown", None
    if approved(conn, domain, base):
        return "live", project
    # Expired: the current build passed its limit, or the last production build was expired.
    cur = conn.execute("SELECT status, expires_at FROM deployments WHERE id = ?",
                       (project["current_deployment_id"],)).fetchone() if project["current_deployment_id"] else None
    if cur is not None and (cur["status"] == "expired" or (cur["expires_at"] and cur["expires_at"] <= utcnow())):
        return "expired", project
    last = conn.execute("SELECT status FROM deployments WHERE project_id = ? AND target = 'production' "
                        "AND status IN ('ready', 'expired') ORDER BY id DESC LIMIT 1", (project["id"],)).fetchone()
    if last is not None and last["status"] == "expired":
        return "expired", project
    return "unavailable", project


def _inline_css() -> Markup:
    # Inlined: the page is served on the app's own hostname, where /static/ is the app's.
    return Markup((Path(current_app.static_folder) / "css" / "site.css").read_text(encoding="utf-8"))


@bp.get("/internal/app-gate")
def app_gate():
    conn = get_db()
    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
    host = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
    state, project = _gate_state(conn, host, _base_domain(conn))
    if state == "live":
        return "", 200
    code = 410 if state == "expired" else 404
    html = render_template("app_unavailable.html", state=state, project=project, css=_inline_css(),
                           home=current_app.config.get("PUBLIC_URL") or "https://codeskys.com")
    return html, code, {"Cache-Control": "no-store", "Content-Type": "text/html; charset=utf-8",
                        "X-Robots-Tag": "noindex"}
