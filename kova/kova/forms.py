"""Project-scoped form endpoints and submission inbox."""
import hashlib
import hmac
import json
import re
import secrets
from flask import Blueprint, current_app, jsonify, request

from .auth import current_user, login_required
from .db import audit, get_db, utcnow
from .ratelimit import rate_limit

bp = Blueprint("forms", __name__, url_prefix="/api")
FIELD_KEY = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,39}$")
FIELD_TYPES = {"text", "email", "textarea", "number", "url", "checkbox"}


def _err(message, code=400):
    return jsonify(error=message), code


def _project(slug):
    user = current_user()
    row = get_db().execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    if row is None or (user["role"] != "admin" and row["owner_id"] != user["id"]):
        return None
    return row


def _form_public(row):
    return {
        "id": row["id"], "name": row["name"], "slug": row["slug"],
        "public_key": row["public_key"], "fields": json.loads(row["fields_json"]),
        "success_message": row["success_message"], "is_active": bool(row["is_active"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "endpoint": f"/api/public/forms/{row['public_key']}",
        "submissions": get_db().execute("SELECT COUNT(*) AS n FROM form_submissions WHERE form_id = ?",
                                         (row["id"],)).fetchone()["n"],
    }


def _clean_fields(fields):
    if not isinstance(fields, list) or not fields or len(fields) > 30:
        raise ValueError("Add between 1 and 30 form fields.")
    out, seen = [], set()
    for item in fields:
        if not isinstance(item, dict):
            raise ValueError("Each field must be an object.")
        key = str(item.get("name", "")).strip()
        label = str(item.get("label", key)).strip()[:80]
        kind = str(item.get("type", "text")).strip().lower()
        if not FIELD_KEY.fullmatch(key) or key in seen:
            raise ValueError("Field names must be unique and use letters, numbers, underscores or hyphens.")
        if kind not in FIELD_TYPES:
            raise ValueError(f"Unsupported field type: {kind}.")
        if not label:
            raise ValueError("Every field needs a label.")
        seen.add(key)
        out.append({"name": key, "label": label, "type": kind,
                    "required": bool(item.get("required", False)),
                    "max_length": max(1, min(4000, int(item.get("max_length", 1000) or 1000)))})
    return out


@bp.get("/projects/<slug>/forms")
@login_required
def list_forms(slug):
    project = _project(slug)
    if project is None:
        return _err("Project not found.", 404)
    rows = get_db().execute("SELECT * FROM project_forms WHERE project_id = ? ORDER BY id DESC",
                            (project["id"],)).fetchall()
    return jsonify(forms=[_form_public(r) for r in rows])


@bp.post("/projects/<slug>/forms")
@login_required
def create_form(slug):
    project = _project(slug)
    if project is None:
        return _err("Project not found.", 404)
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:100]
    if not name:
        return _err("Enter a form name.")
    try:
        fields = _clean_fields(data.get("fields"))
    except (ValueError, TypeError) as exc:
        return _err(str(exc))
    base = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:50] or "form"
    db = get_db()
    slug_part, count = base, 2
    while db.execute("SELECT 1 FROM project_forms WHERE project_id = ? AND slug = ?",
                     (project["id"], slug_part)).fetchone():
        slug_part = f"{base}-{count}"; count += 1
    now = utcnow()
    cur = db.execute("INSERT INTO project_forms (project_id, name, slug, public_key, fields_json, success_message, "
                      "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                      (project["id"], name, slug_part, secrets.token_urlsafe(32), json.dumps(fields),
                       str(data.get("success_message") or "Thanks — your response has been received.")[:240], now, now))
    db.commit()
    audit("form.created", actor_id=current_user()["id"], target=name, project_id=project["id"])
    row = db.execute("SELECT * FROM project_forms WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(form=_form_public(row)), 201


@bp.patch("/projects/<slug>/forms/<int:form_id>")
@login_required
def update_form(slug, form_id):
    project = _project(slug)
    if project is None:
        return _err("Project not found.", 404)
    db = get_db()
    row = db.execute("SELECT * FROM project_forms WHERE id = ? AND project_id = ?",
                     (form_id, project["id"])).fetchone()
    if row is None:
        return _err("Form not found.", 404)
    data = request.get_json(silent=True) or {}
    fields = json.loads(row["fields_json"])
    if "fields" in data:
        try:
            fields = _clean_fields(data["fields"])
        except (ValueError, TypeError) as exc:
            return _err(str(exc))
    name = str(data.get("name", row["name"])).strip()[:100]
    message = str(data.get("success_message", row["success_message"]))[:240]
    active = 1 if data.get("is_active", bool(row["is_active"])) else 0
    if not name:
        return _err("Enter a form name.")
    db.execute("UPDATE project_forms SET name=?, fields_json=?, success_message=?, is_active=?, updated_at=? WHERE id=?",
               (name, json.dumps(fields), message, active, utcnow(), form_id))
    db.commit()
    audit("form.updated", actor_id=current_user()["id"], target=name, project_id=project["id"])
    return jsonify(form=_form_public(db.execute("SELECT * FROM project_forms WHERE id = ?", (form_id,)).fetchone()))


@bp.delete("/projects/<slug>/forms/<int:form_id>")
@login_required
def delete_form(slug, form_id):
    project = _project(slug)
    if project is None:
        return _err("Project not found.", 404)
    db = get_db()
    row = db.execute("SELECT name FROM project_forms WHERE id = ? AND project_id = ?",
                     (form_id, project["id"])).fetchone()
    if row is None:
        return _err("Form not found.", 404)
    db.execute("DELETE FROM project_forms WHERE id = ?", (form_id,))
    db.commit()
    audit("form.deleted", actor_id=current_user()["id"], target=row["name"], project_id=project["id"])
    return jsonify(ok=True)


@bp.get("/projects/<slug>/forms/<int:form_id>/submissions")
@login_required
def form_submissions(slug, form_id):
    project = _project(slug)
    if project is None:
        return _err("Project not found.", 404)
    db = get_db()
    form = db.execute("SELECT id FROM project_forms WHERE id = ? AND project_id = ?",
                      (form_id, project["id"])).fetchone()
    if form is None:
        return _err("Form not found.", 404)
    rows = db.execute("SELECT * FROM form_submissions WHERE form_id = ? ORDER BY id DESC LIMIT 500",
                      (form_id,)).fetchall()
    return jsonify(submissions=[{"id": r["id"], "data": json.loads(r["payload_json"]),
                                 "created_at": r["created_at"], "user_agent": r["user_agent"]}
                                for r in rows], total=db.execute(
                                    "SELECT COUNT(*) AS n FROM form_submissions WHERE form_id = ?",
                                    (form_id,)).fetchone()["n"])


@bp.post("/public/forms/<public_key>")
def submit_public_form(public_key):
    if request.content_length and request.content_length > 32768:
        return _err("Submission is too large.", 413)
    db = get_db()
    form = db.execute("SELECT * FROM project_forms WHERE public_key = ? AND is_active = 1",
                      (public_key,)).fetchone()
    if form is None:
        return _err("This form is unavailable.", 404)
    ip = request.remote_addr or "unknown"
    if rate_limit(db, f"public-form:{form['id']}:{ip}", limit=10, per_seconds=60):
        return _err("Too many submissions. Please try again shortly.", 429)
    incoming = request.get_json(silent=True)
    if incoming is None:
        incoming = request.form.to_dict(flat=True)
    if not isinstance(incoming, dict):
        return _err("Send form data as a JSON object or form fields.")
    # Optional honeypot field for basic bot mitigation.
    if incoming.get("_kova_website"):
        return jsonify(ok=True, message="Thanks.")
    fields = json.loads(form["fields_json"])
    cleaned = {}
    errors = []
    for field in fields:
        key = field["name"]
        value = incoming.get(key)
        if field["type"] == "checkbox":
            value = bool(value) if value is not None else False
            if field["required"] and not value:
                errors.append(f"{field['label']} is required.")
            cleaned[key] = value
            continue
        value = "" if value is None else str(value).strip()
        if field["required"] and not value:
            errors.append(f"{field['label']} is required.")
        if len(value) > field["max_length"]:
            errors.append(f"{field['label']} is too long.")
        if field["type"] == "email" and value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            errors.append(f"{field['label']} must be a valid email address.")
        cleaned[key] = value
    if errors:
        return jsonify(error=" ".join(errors)), 422
    secret = current_app.config.get("SECRET_KEY", "")
    ip_hash = hmac.new(secret.encode(), ip.encode(), hashlib.sha256).hexdigest() if secret else ""
    db.execute("INSERT INTO form_submissions (form_id, project_id, payload_json, ip_hash, user_agent, created_at) "
               "VALUES (?,?,?,?,?,?)", (form["id"], form["project_id"], json.dumps(cleaned, ensure_ascii=False),
                                          ip_hash, request.headers.get("User-Agent", "")[:300], utcnow()))
    db.commit()
    return jsonify(ok=True, message=form["success_message"])
