"""Serves simulator deployments so every Codeskys URL resolves before Coolify is connected."""
from flask import Blueprint, abort, render_template, request

from .db import get_db
from .gitinfo import FRAMEWORKS

bp = Blueprint("sites", __name__)


@bp.get("/sites/<slug>/")
def site(slug):
    db = get_db()
    p = db.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    if p is None:
        abort(404)
    dep_id = request.args.get("deploy", type=int) or p["current_deployment_id"]
    d = db.execute("SELECT * FROM deployments WHERE id = ? AND project_id = ? AND status = 'ready'",
                   (dep_id, p["id"])).fetchone() if dep_id else None
    if d is None:
        return render_template("site_missing.html", project=p), 404
    return render_template("site.html", project=p, dep=d,
                           framework=FRAMEWORKS.get(p["framework"], FRAMEWORKS["static"])["name"],
                           is_preview=d["target"] == "preview" or d["id"] != p["current_deployment_id"])
