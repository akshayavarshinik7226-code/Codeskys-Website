"""Direct file upload: a zip of a finished website, hosted by Codeskys itself.

Why this exists: a project does not have to come from GitHub. A user can upload a
.zip of a finished static site (HTML/CSS/JS, or the built ``dist`` / ``build``
folder of a React, Vue or Vite project). Codeskys checks the zip, unpacks it into
``<instance>/uploads/<project id>/<deployment id>/`` and serves it directly, so this
path produces a REAL live link without Coolify. (Back-end apps still need the
Coolify server; they are refused here with a plain message.)

Safety rules, all enforced before a single file is published:

* the zip must be a real zip and within the size / file-count limits;
* a path that tries to leave the project folder (``../x``, ``/etc/x``, ``C:\\x``) rejects
  the whole upload;
* symbolic links and password-protected zips are rejected;
* ``.git``, ``node_modules``, ``.env*`` files, macOS junk and executables are never
  published (the first three would leak secrets or waste disk);
* there must be an ``index.html`` (at the top, inside a single wrapper folder, or in a
  ``dist`` / ``build`` / ``public`` folder);
* uploaded pages are served with a CSP ``sandbox`` so they can never read the
  dashboard's session, even when served from the same origin as the dashboard.
"""
import hashlib
import os
import re
import secrets
import shutil
import time
import zipfile
from pathlib import Path

from flask import (Blueprint, current_app, g, jsonify, redirect, render_template, request,
                   send_from_directory)

from . import engine
from .auth import current_user, login_required
from .db import audit, get_db, utcnow
from .projects import (PLAN_LIMITS, _clean_ttl, _dep_public, _load_project, _project_public,
                       _system_domain, _unique_slug, err, in_flight, slugify)
from .ratelimit import rate_limit

bp = Blueprint("uploads", __name__)

# Never published. Directory names are matched anywhere in the path.
SKIP_DIRS = {".git", "node_modules", "__macosx", ".svn", ".hg"}
SKIP_FILES = {".ds_store", "thumbs.db"}
BLOCKED_EXT = {".exe", ".dll", ".bat", ".cmd", ".com", ".msi", ".scr", ".ps1", ".vbs", ".jar",
               ".apk", ".dmg", ".sh", ".bin"}
BUILD_DIRS = ("dist", "build", "public", "out", "www", "site", "_site")
# Slugs that would collide with real services once links become <name>.<domain>.
RESERVED_SLUGS = {"www", "api", "admin", "app", "apps", "mail", "ftp", "dashboard", "login", "static",
                  "live", "sites", "docs", "status", "blog", "help", "support", "billing", "coolify",
                  "authz", "smtp", "imap", "cdn", "ns1", "ns2", "assets", "codeskys", "kova"}
CUSTOM_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$")


class UploadError(ValueError):
    """A problem with the uploaded files, always phrased for the person who uploaded."""


# ------------------------------------------------------------------ paths

def upload_root(cfg) -> Path:
    return Path(cfg["INSTANCE_DIR"]) / "uploads"


def staged_zip(cfg, project_id, dep_id) -> Path:
    return upload_root(cfg) / str(project_id) / f"{dep_id}.zip"


def site_dir(cfg, project_id, dep_id) -> Path:
    return upload_root(cfg) / str(project_id) / str(dep_id)


def safe_site_path(cfg, rel: str):
    """Resolve a stored relative path, refusing anything outside the uploads folder."""
    if not rel:
        return None
    base = upload_root(cfg).resolve()
    path = (base / rel).resolve()
    if path != base and base not in path.parents:
        return None
    return path


def site_url(cfg, project_slug: str) -> str:
    """The address a visitor uses. A real subdomain when a wildcard domain is configured."""
    domain = cfg.get("SITES_DOMAIN")
    if domain:
        public = cfg.get("PUBLIC_URL", "")
        scheme = "http" if public.startswith("http://") or not cfg.get("SESSION_COOKIE_SECURE") else "https"
        return f"{scheme}://{project_slug}.{domain}/"
    return f"/live/{project_slug}/"


# ------------------------------------------------------------------ unpacking

def _clean_member(name: str):
    name = name.replace("\\", "/")
    if "\x00" in name:
        raise UploadError("The zip contains a file with an invalid name, so it was rejected.")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise UploadError("The zip contains an absolute file path, so it was rejected.")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UploadError("The zip contains a file path that goes outside the project folder, so it was rejected.")
    return parts


def _skip_reason(parts, is_dir):
    lowered = [p.lower() for p in parts]
    for p in lowered[: len(lowered) if is_dir else len(lowered) - 1]:
        if p in SKIP_DIRS:
            return f"folder '{p}' is never published"
    if not is_dir:
        name = lowered[-1]
        if name in SKIP_FILES:
            return None  # silent junk
        if name.startswith(".env"):   # .env, .env.local, .envrc
            return "environment file (it would expose secrets on a public website)"
        if os.path.splitext(name)[1] in BLOCKED_EXT:
            return "executable files are not allowed"
    return None


def unpack(zip_path: Path, dest: Path, *, max_bytes: int, max_files: int, log=None):
    """Safely extract ``zip_path`` into ``dest`` and return ``(site_root, stats)``."""
    log = log or (lambda msg, level="info": None)
    if not zipfile.is_zipfile(zip_path):
        raise UploadError("That file is not a valid .zip. Compress your project folder as a ZIP and upload again.")
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    written = 0
    count = 0
    skipped = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if not infos:
                raise UploadError("The zip is empty.")
            if len(infos) > max_files:
                raise UploadError(f"The zip has {len(infos):,} files; the limit is {max_files:,}. "
                                  "Upload the built site, not the whole project with its dependencies.")
            declared = sum(i.file_size for i in infos)
            if declared > max_bytes:
                raise UploadError(f"The files add up to {declared // (1024 * 1024)} MB; the limit is "
                                  f"{max_bytes // (1024 * 1024)} MB.")
            for info in infos:
                if info.flag_bits & 0x1:
                    raise UploadError("The zip is password-protected. Upload an unprotected zip.")
                if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    raise UploadError("The zip contains a symbolic link, which is not allowed.")
                parts = _clean_member(info.filename)
                if not parts:
                    continue
                reason = _skip_reason(parts, info.is_dir())
                if reason:
                    skipped.append(f"{'/'.join(parts)} — {reason}")
                    continue
                if (not info.is_dir()) and parts[-1].lower() in SKIP_FILES:
                    continue
                target = base.joinpath(*parts)
                resolved = target.resolve()
                if resolved != base and base not in resolved.parents:
                    raise UploadError("The zip contains a file path that goes outside the project folder, "
                                      "so it was rejected.")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    while True:
                        chunk = src.read(1024 * 256)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise UploadError(f"The files are larger than the {max_bytes // (1024 * 1024)} MB limit.")
                        out.write(chunk)
                count += 1
    except zipfile.BadZipFile:
        raise UploadError("The zip is damaged. Create it again and upload again.")
    for line in skipped[:20]:
        log(f"Skipped {line}", "warn")
    if len(skipped) > 20:
        log(f"…and {len(skipped) - 20} more skipped files", "warn")
    if count == 0:
        raise UploadError("The zip has no publishable files.")
    root = find_site_root(dest)
    return root, {"files": count, "bytes": written, "skipped": len(skipped)}


def find_site_root(dest: Path) -> Path:
    """Where ``index.html`` lives: the top, one wrapper folder down, or a build folder."""
    root = dest
    for _ in range(3):
        if (root / "index.html").is_file():
            return root
        kids = list(root.iterdir())
        dirs = [k for k in kids if k.is_dir()]
        files = [k for k in kids if k.is_file()]
        if len(dirs) == 1 and not files:
            root = dirs[0]
            continue
        break
    for name in BUILD_DIRS:
        if (root / name / "index.html").is_file():
            return root / name
    raise UploadError("No index.html was found. A website needs an index.html at the top of the zip "
                      "(or inside a dist / build folder). If your project needs a build step, "
                      "run it first and zip the output folder.")


def remove_deployment_files(cfg, dep) -> bool:
    """Delete a deployment's files from disk. Returns True if anything was removed."""
    removed = False
    path = site_dir(cfg, dep["project_id"], dep["id"])
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        removed = True
    zp = staged_zip(cfg, dep["project_id"], dep["id"])
    if zp.exists():
        zp.unlink(missing_ok=True)
        removed = True
    return removed


def discard_deployment(conn, cfg, dep):
    """A static deployment that will never serve (failed, cancelled, expired): free its disk."""
    removed = remove_deployment_files(cfg, dep)
    if dep["source_dir"]:
        conn.execute("UPDATE deployments SET source_dir = '' WHERE id = ?", (dep["id"],))
        conn.commit()
    return removed


INCOMING_MAX_AGE = 3600   # an upload request that died between receiving and queueing


def sweep_files(conn, cfg) -> int:
    """Catch-up for the scheduler: files of finished static deployments, abandoned uploads.

    Idempotent. Only deployments that can no longer serve are touched; a queued or building
    upload keeps its staged zip, and the live deployment keeps its files."""
    cleaned = 0
    rows = conn.execute("SELECT * FROM deployments WHERE provider = ? AND status IN ('failed','cancelled','expired') "
                        "ORDER BY id DESC LIMIT 500", (engine.STATIC,)).fetchall()
    for dep in rows:
        if discard_deployment(conn, cfg, dep):
            cleaned += 1
    incoming = upload_root(cfg) / "_incoming"
    if incoming.is_dir():
        cutoff = time.time() - INCOMING_MAX_AGE
        for f in incoming.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    cleaned += 1
            except OSError:
                pass
    return cleaned


def remove_project_files(cfg, project_id):
    shutil.rmtree(upload_root(cfg) / str(project_id), ignore_errors=True)


def cleanup_superseded(conn, cfg, project, keep_dep_id):
    """After a newer upload goes live, free the disk used by the older ones."""
    rows = conn.execute("SELECT * FROM deployments WHERE project_id = ? AND provider = ? AND id != ? "
                        "AND source_dir != ''", (project["id"], engine.STATIC, keep_dep_id)).fetchall()
    for row in rows:
        remove_deployment_files(cfg, row)
        conn.execute("UPDATE deployments SET source_dir = '', teardown_note = ? WHERE id = ?",
                     ("Replaced by a newer upload; its files were removed.", row["id"]))
    if rows:
        conn.commit()


# ------------------------------------------------------------------ serving

def host_site_slug(cfg):
    """With a wildcard domain configured, ``<name>.<domain>`` selects a project."""
    domain = cfg.get("SITES_DOMAIN")
    if not domain:
        return None
    host = request.host.split(":")[0].lower()
    if host.endswith("." + domain):
        label = host[: -len(domain) - 1]
        if label and "." not in label:
            return label
    return None


def _not_live(project, status=404):
    g.live_site = True
    resp = current_app.response_class(
        render_template("site_expired.html", project=project), status=status, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def serve_site(slug: str, subpath: str):
    """Serve one file of a project's current live upload, or a 404 once it has expired."""
    g.live_site = True
    if request.method not in ("GET", "HEAD"):
        return current_app.response_class("Method not allowed", status=405)
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
    dep = None
    if project is not None and project["current_deployment_id"]:
        dep = db.execute("SELECT * FROM deployments WHERE id = ? AND project_id = ? AND status = 'ready' "
                         "AND provider = ?", (project["current_deployment_id"], project["id"], engine.STATIC)).fetchone()
    if dep is None or (dep["expires_at"] and dep["expires_at"] <= utcnow()) or project["is_paused"]:
        return _not_live(project)
    root = safe_site_path(current_app.config, dep["source_dir"])
    if root is None or not root.is_dir():
        return _not_live(project)
    rel = subpath.strip("/")
    target = (root / rel).resolve() if rel else root
    if target != root and root not in target.parents:
        return _not_live(project)
    if target.is_dir():
        if rel and not request.path.endswith("/"):
            return redirect(request.path + "/", 301)
        target = target / "index.html"
        rel = f"{rel}/index.html" if rel else "index.html"
    status = 200
    if not target.is_file():
        custom = root / "404.html"
        if custom.is_file():
            rel, status = "404.html", 404
        else:
            return current_app.response_class("404 — page not found", status=404, mimetype="text/plain")
    resp = send_from_directory(root, rel, conditional=True, max_age=0)
    resp.status_code = status if status != 200 else resp.status_code
    by_host = bool(host_site_slug(current_app.config))
    flags = "allow-scripts allow-forms allow-popups allow-modals allow-downloads"
    # On a path under the dashboard's own origin, the page must not be able to act as the
    # signed-in user, so it runs in an opaque origin. Its own subdomain is already isolated.
    resp.headers["Content-Security-Policy"] = "sandbox " + flags + (" allow-same-origin" if by_host else "")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@bp.get("/live/<slug>/", defaults={"subpath": ""})
@bp.get("/live/<slug>/<path:subpath>")
def live(slug, subpath):
    return serve_site(slug, subpath)


@bp.get("/live/<slug>")
def live_redirect(slug):
    return redirect(f"/live/{slug}/", 301)


# ------------------------------------------------------------------ upload API

def _sha1_and_size(path: Path):
    h, size = hashlib.sha1(), 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _receive_zip():
    """Save the uploaded file to a temporary place and make sure it is a zip."""
    f = request.files.get("file")
    if f is None or not f.filename:
        raise UploadError("Choose a .zip file to upload.")
    if not f.filename.lower().endswith(".zip"):
        raise UploadError("Only .zip files can be uploaded. Compress your project folder as a ZIP first.")
    incoming = upload_root(current_app.config) / "_incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    tmp = incoming / f"{secrets.token_hex(12)}.zip"
    f.save(tmp)
    limit = current_app.config["MAX_UPLOAD_MB"] * 1024 * 1024
    if tmp.stat().st_size > limit:
        tmp.unlink(missing_ok=True)
        raise UploadError(f"The zip is larger than the {current_app.config['MAX_UPLOAD_MB']} MB limit.")
    if not zipfile.is_zipfile(tmp):
        tmp.unlink(missing_ok=True)
        raise UploadError("That file is not a valid .zip. Compress your project folder as a ZIP and upload again.")
    return tmp, f.filename


def _queue_upload(db, project, user_id, tmp: Path, filename: str):
    """Create the deployment row, move the zip next to it and hand it to the queue."""
    cfg = current_app.config
    digest, size = _sha1_and_size(tmp)
    same = in_flight(db, project["id"], "production", "upload", digest)
    if same is not None:            # the same file sent twice (double click, retry)
        tmp.unlink(missing_ok=True)
        return same
    number = db.execute("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM deployments WHERE project_id = ?",
                        (project["id"],)).fetchone()["n"]
    cur = db.execute(
        "INSERT INTO deployments (project_id, number, status, stage, target, branch, commit_sha, commit_message, "
        "trigger, triggered_by, provider, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (project["id"], number, "queued", "queued", "production", "upload", digest,
         f"Uploaded {os.path.basename(filename)[:80]} ({_human(size)})", "upload", user_id, engine.STATIC, utcnow()))
    dep_id = cur.lastrowid
    dest = staged_zip(cfg, project["id"], dep_id)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(dest))
    except OSError:
        db.execute("DELETE FROM deployments WHERE id = ?", (dep_id,))
        db.commit()
        raise UploadError("The server could not store your upload. Please try again.")
    db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (utcnow(), project["id"]))
    db.execute("INSERT INTO deploy_logs (deployment_id, ts, level, stage, line) VALUES (?,?,?,?,?)",
               (dep_id, utcnow(), "info", "queued",
                f"Upload #{number} received ({_human(size)}). Waiting for a free build slot…"))
    db.commit()
    engine.enqueue_deploy(db, dep_id)
    return dep_id


@bp.post("/api/projects/upload")
@login_required
def create_from_upload():
    user, db = current_user(), get_db()
    if rate_limit(db, f"upload:{user['id']}", limit=20, per_seconds=300):
        return err("You're uploading very frequently. Wait a moment and try again.", 429)
    name = str(request.form.get("name") or "").strip()[:64]
    if not name:
        return err("Give your project a name.")
    limit = PLAN_LIMITS.get(user["plan"], PLAN_LIMITS["hobby"])["projects"]
    if user["role"] != "admin":
        count = db.execute("SELECT COUNT(*) AS n FROM projects WHERE owner_id = ?", (user["id"],)).fetchone()["n"]
        if count >= limit:
            return err(f"Your {user['plan'].title()} plan allows {limit} projects. Delete one or upgrade.", 402)
    custom = str(request.form.get("slug") or "").strip().lower()
    if custom:
        if not CUSTOM_SLUG_RE.match(custom) or "--" in custom:
            return err("A link name can use lowercase letters, numbers and single hyphens (3-48 characters).", 422)
        if custom in RESERVED_SLUGS:
            return err("That link name is reserved. Choose another.", 422)
        if db.execute("SELECT 1 FROM projects WHERE slug = ?", (custom,)).fetchone():
            return err("That link name is already taken. Choose another.", 409)
        slug = custom
    else:
        base = slugify(name)
        slug = _unique_slug(db, base + "-site" if base in RESERVED_SLUGS else base)
    try:
        tmp, filename = _receive_zip()
    except UploadError as exc:
        return err(str(exc), 422)
    ttl, _ = _clean_ttl(None, db)
    now = utcnow()
    cfg = current_app.config
    cur = db.execute(
        "INSERT INTO projects (owner_id, name, slug, description, repo_url, repo_full_name, branch, framework, "
        "port, hook_token, webhook_secret, ttl_hours, limits_memory, limits_cpus, source_type, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], name, slug, str(request.form.get("description") or "")[:300], f"upload://{slug}",
         "Uploaded files", "upload", "static", 80, secrets.token_urlsafe(24), secrets.token_urlsafe(32), ttl,
         cfg.get("BUILD_MEMORY", "512M"), cfg.get("BUILD_CPUS", "0.5"), "upload", now, now))
    pid = cur.lastrowid
    if cfg.get("SITES_DOMAIN"):
        db.execute("INSERT INTO domains (project_id, hostname, is_primary, is_system, status, verify_token, "
                   "created_at, verified_at) VALUES (?,?,?,?,?,?,?,?)",
                   (pid, f"{slug}.{cfg['SITES_DOMAIN']}", 1, 1, "active", secrets.token_hex(8), now, now))
    db.commit()
    project = db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    try:
        dep_id = _queue_upload(db, project, user["id"], tmp, filename)
    except UploadError as exc:
        db.execute("DELETE FROM projects WHERE id = ?", (pid,))
        db.commit()
        return err(str(exc), 500)
    audit("project.created", actor_id=user["id"], target=slug, ip=request.remote_addr, project_id=pid,
          detail="file upload")
    project = db.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    return jsonify(project=_project_public(project, db, full=True), deployment_id=dep_id), 201


@bp.post("/api/projects/<slug>/upload")
@login_required
def reupload(slug):
    """Upload a new version. Free projects keep their original 24-hour expiry."""
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    if p["source_type"] != "upload":
        return err("This project deploys from a Git repository. Push to the repository to update it.", 409)
    if p["is_paused"]:
        return err("This project is paused. Resume it in Settings to upload.", 409)
    user, db = current_user(), get_db()
    if rate_limit(db, f"upload:{user['id']}", limit=20, per_seconds=300):
        return err("You're uploading very frequently. Wait a moment and try again.", 429)
    try:
        tmp, filename = _receive_zip()
        dep_id = _queue_upload(db, p, user["id"], tmp, filename)
    except UploadError as exc:
        return err(str(exc), 422)
    audit("deploy.triggered", actor_id=user["id"], target=f"#{dep_id}", project_id=p["id"], detail="file upload")
    dep = db.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    return jsonify(deployment=_dep_public(dep)), 201
