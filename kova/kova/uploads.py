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
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import tarfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Blueprint, current_app, g, jsonify, redirect, render_template, request,
                   send_file, send_from_directory)

from . import engine
from .auth import current_user, login_required
from .db import audit, get_db, utcnow
from . import plans
from .plans import PlanBlocked
from .projects import (CUSTOM_SLUG_RE, PLAN_LIMITS, RESERVED_SLUGS, _clean_ttl, _dep_public, _load_project,
                       _project_public, _system_domain, _unique_slug, err, in_flight, slugify)
from .ratelimit import rate_limit

bp = Blueprint("uploads", __name__)

# Never published. Directory names are matched anywhere in the path.
SKIP_DIRS = {".git", "node_modules", "__macosx", ".svn", ".hg"}
SKIP_FILES = {".ds_store", "thumbs.db"}
BLOCKED_EXT = {".exe", ".dll", ".bat", ".cmd", ".com", ".msi", ".scr", ".ps1", ".vbs", ".jar",
               ".apk", ".dmg", ".sh", ".bin"}
BUILD_DIRS = ("dist", "build", "public", "out", "www", "site", "_site")
# RESERVED_SLUGS and CUSTOM_SLUG_RE (what a link name may be) live in projects.py so that
# renaming a project and uploading one follow exactly the same rules.


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


def unpack(zip_path: Path, dest: Path, *, max_bytes: int, max_files: int, log=None, site=True):
    """Safely extract ``zip_path`` into ``dest`` and return ``(site_root, stats)``.
    With ``site=False`` (source for a build) no index.html is needed and ``dest`` is returned."""
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
    root = find_site_root(dest) if site else dest
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
    files = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    from .projectscan import choose, refusal
    if not any(f.endswith((".html", ".htm")) for f in files) and choose(files, lambda _p: None) is None:
        raise UploadError(refusal(files))
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
    extra = sorted((upload_root(cfg) / str(dep["project_id"])).glob(f"{dep['id']}.src*"))
    for zp in (staged_zip(cfg, dep["project_id"], dep["id"]), *_source_files(cfg, dep["project_id"], dep["id"]),
               *extra):
        if zp.exists():
            try:
                zp.unlink(missing_ok=True)
                removed = True
            except OSError:
                pass                 # still open somewhere: the scheduler's sweep retries
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
    # Uploaded source built by the deployment engine is only needed while it builds.
    rows = conn.execute("SELECT * FROM deployments WHERE provider = ? AND trigger = 'upload' "
                        "AND status NOT IN ('queued','building') ORDER BY id DESC LIMIT 500",
                        (engine.COOLIFY,)).fetchall()
    for dep in rows:
        if remove_deployment_files(cfg, dep):
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


# ------------------------------------------------------------------ source projects
#
# A zip that holds an application (package.json, requirements.txt, manage.py or a
# Dockerfile) instead of a finished site is built by the deployment engine exactly like
# a GitHub repository: same framework presets, generated Dockerfile, build policy and
# isolated service. Only the build context differs. The worker unpacks the zip with the
# rules above, packs it as a .tar.gz and the build server fetches it once or twice from
# /internal/build-source/<deployment>/<token>.tar.gz:
#
# * /internal* is blocked on the public site by Caddy, and a request that came through a
#   proxy (X-Forwarded-For / Forwarded) is refused here as well;
# * KOVA_BUILD_SOURCE_PEERS limits the callers to the build server's address;
# * the token is an HMAC of the deployment id and the archive's sha256 (never stored,
#   never shown to the student), and it only works while that deployment is building,
#   within BUILD_TIMEOUT, for at most SOURCE_FETCH_LIMIT requests;
# * the archive is deleted as soon as the deployment ends (ready, failed or cancelled).

SOURCE_MARKERS = ("package.json", "requirements.txt", "manage.py", "Dockerfile")
MAX_MANIFEST_BYTES = 512 * 1024
SOURCE_FETCH_LIMIT = 10          # BuildKit reads a remote context twice per build
SECRET_SCAN_MAX_FILE = 1024 * 1024
SECRET_SCAN_MAX_TOTAL = 64 * 1024 * 1024
_SECRET_PATTERNS = (
    ("a certificate-style key block", re.compile(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("an AWS access key id", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("an OpenAI-style API key", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("a Slack token", re.compile(rb"\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("a Google API key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
)


def _source_files(cfg, project_id, dep_id, n: int = 0):
    """(archive, sha256 file, fetch counter) of build context ``n`` (0 = the whole project)."""
    base, tag = upload_root(cfg) / str(project_id), f"{dep_id}.src" + (f"-{int(n)}" if n else "")
    return base / f"{tag}.tar.gz", base / f"{tag}.sha256", base / f"{tag}.fetches"


def _members(zf):
    """Relative paths of the files that would be extracted (the same skip rules as unpack)."""
    out = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        try:
            parts = _clean_member(info.filename)
        except UploadError:
            continue
        if parts and not _skip_reason(parts, False) and parts[-1].lower() not in SKIP_FILES:
            out.append(("/".join(parts), info))
    return out


def _source_root(paths) -> str:
    """The folder holding the project: the top, or up to three single wrapper folders down."""
    prefix = ""
    for _ in range(4):
        rel = [p[len(prefix):] for p in paths if p.startswith(prefix)]
        if any("/" not in p for p in rel):
            return prefix
        dirs = {p.split("/", 1)[0] for p in rel}
        if len(dirs) != 1:
            return prefix
        prefix += dirs.pop() + "/"
    return prefix


def source_plan(zip_path: Path, root_dir: str = ""):
    """``{"root", "framework"}`` when the zip is a project the deployment engine must build,
    None when it is a finished site (served directly, as before). ``root_dir`` is the project's
    Root directory setting, inside the upload. Never raises."""
    from .gitinfo import FRAMEWORKS
    from .projectscan import choose
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = dict(_members(zf))
            root = _source_root(list(members))
            sub = (root_dir or "").strip("/")
            base = root + (sub + "/" if sub else "")
            rel = {p[len(base):] for p in members if p.startswith(base)}

            def read(name):
                info = members.get(base + name)
                if info is None or info.file_size > MAX_MANIFEST_BYTES:
                    return None
                return zf.read(info).decode("utf-8", errors="replace")
            framework, inner_root = choose(rel, read), ""
            if framework in (None, "docs") and not sub:
                # Nothing to run at the top, but an app one or two folders down (backend/, app/web/).
                from .projectscan import nested_root
                inner_root = nested_root(rel)
                if inner_root:
                    inner = choose({p[len(inner_root) + 1:] for p in rel if p.startswith(inner_root + "/")},
                                   lambda p: read(f"{inner_root}/{p}"))
                    if inner and inner not in ("docs", "static"):
                        return {"root": root.rstrip("/"), "framework": inner, "root_dir": inner_root}
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return None
    fw = FRAMEWORKS.get(framework or "")
    if fw is None or framework == "static":
        return None
    if fw["static"] and fw["output"] and f"{fw['output']}/index.html" in rel:
        return None          # the built site is already in the zip: publish it, as before
    return {"root": root.rstrip("/"), "framework": framework}


def source_builds_enabled(conn, cfg) -> bool:
    """Source zips are built only when the real engine and the build-source address are set."""
    return bool(cfg.get("BUILD_SOURCE_URL")) and engine.active_provider(conn, cfg) == engine.COOLIFY


def framework_family(framework: str) -> str:
    """Presets that build and run the same way (a student's own settings carry over inside one)."""
    from .dockerfiles import PYTHON_FRAMEWORKS
    from .gitinfo import FRAMEWORKS
    if framework in ("docker", "static") or framework not in FRAMEWORKS:
        return framework
    if framework in PYTHON_FRAMEWORKS:
        return "python"
    return "node-site" if FRAMEWORKS[framework]["static"] else "node-app"


def build_source_token(cfg, dep_id: int, digest: str, n: int = 0) -> str:
    key = hashlib.sha256(b"codeskys-build-source\0" + str(cfg["SECRET_KEY"]).encode()).digest()
    msg = f"{int(dep_id)}:{digest}" if not n else f"{int(dep_id)}:{int(n)}:{digest}"
    mac = hmac.new(key, msg.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def build_source_url(cfg, dep_id: int, digest: str, n: int = 0) -> str:
    return (f"{cfg['BUILD_SOURCE_URL'].rstrip('/')}/internal/build-source/{int(dep_id)}/"
            f"{build_source_token(cfg, dep_id, digest, n)}" + (f"-{int(n)}" if n else "") + ".tar.gz")


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stage_subcontext(cfg, project, dep, folder: str, n: int) -> str:
    """Build context ``n``: the ``folder`` of the staged project, as its own archive (a
    multi-service project builds each service from its own folder). Returns its URL."""
    folder = folder.strip("/")
    whole = _source_files(cfg, project["id"], dep["id"])[0]
    tar_path, sha_path, count_path = _source_files(cfg, project["id"], dep["id"], n)
    count = 0
    with tarfile.open(whole, "r:gz") as src, tarfile.open(tar_path, "w:gz", compresslevel=6) as out:
        for member in src.getmembers():
            if member.isfile() and member.name.startswith(folder + "/"):
                info = tarfile.TarInfo(member.name[len(folder) + 1:])
                info.size, info.mode, info.mtime = member.size, member.mode, member.mtime
                out.addfile(info, src.extractfile(member))
                count += 1
    if not count:
        tar_path.unlink(missing_ok=True)
        raise UploadError(f"The folder {folder}/ is empty or not in the upload.")
    digest = _digest(tar_path)
    sha_path.write_text(digest)
    count_path.unlink(missing_ok=True)
    return build_source_url(cfg, dep["id"], digest, n)


def scan_secrets(root: Path):
    """``[(relative path, kind)]`` of files that look like they hold a live key or token."""
    found, total = [], 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        size = path.stat().st_size
        if size > SECRET_SCAN_MAX_FILE or total + size > SECRET_SCAN_MAX_TOTAL:
            continue
        total += size
        data = path.read_bytes()
        for kind, pattern in _SECRET_PATTERNS:
            if pattern.search(data):
                found.append((path.relative_to(root).as_posix(), kind))
                break
    return found


def stage_build_source(cfg, project, dep, log) -> dict:
    """Unpack the deployment's zip and pack the build context. Raises UploadError."""
    zip_path = staged_zip(cfg, project["id"], dep["id"])
    if not zip_path.is_file():
        raise UploadError("The uploaded file is missing on the server. Please upload again.")
    dest = site_dir(cfg, project["id"], dep["id"])
    _, stats = unpack(zip_path, dest, max_bytes=int(cfg["MAX_UPLOAD_MB"]) * 1024 * 1024,
                      max_files=int(cfg["MAX_UPLOAD_FILES"]), log=log, site=False)
    root = dest
    with zipfile.ZipFile(zip_path) as zf:
        plan_root = _source_root([p for p, _ in _members(zf)]).rstrip("/")
    if plan_root:
        root = dest.joinpath(*plan_root.split("/"))
    sub = (project["root_dir"] or "").strip("/")
    if sub:
        root = root.joinpath(*sub.split("/"))
        if not root.is_dir():
            raise UploadError(f"The root directory {sub}/ is not in the uploaded files. "
                              "Fix it in Project → Settings, or upload again.")
    for rel, kind in scan_secrets(root)[:10]:
        log(f"Warning: {rel} looks like it contains {kind}. It becomes part of your app; keep such values in "
            "Project → Settings → Environment variables instead.", "warn")
    tar_path, sha_path, count_path = _source_files(cfg, project["id"], dep["id"])
    files = set()
    with tarfile.open(tar_path, "w:gz", compresslevel=6) as tar:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            info = tar.gettarinfo(str(path), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if info.mode & 0o111 else 0o644
            with open(path, "rb") as fh:
                tar.addfile(info, fh)
            files.add(rel)
    if not files:
        raise UploadError("The uploaded project folder is empty.")
    digest = _digest(tar_path)
    sha_path.write_text(digest)
    count_path.unlink(missing_ok=True)
    shutil.rmtree(dest, ignore_errors=True)        # the archive is all the build needs
    return {"url": build_source_url(cfg, dep["id"], digest), "files": files, "root": root,
            "count": len(files), "bytes": tar_path.stat().st_size, "skipped": stats["skipped"]}


def read_source_file(cfg, project, dep, name: str):
    """One file of the staged build context as text (e.g. the Dockerfile), or None."""
    tar_path = _source_files(cfg, project["id"], dep["id"])[0]
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            member = tar.getmember(name)
            if not member.isfile() or member.size > MAX_MANIFEST_BYTES:
                return None
            return tar.extractfile(member).read().decode("utf-8", errors="replace")
    except (KeyError, OSError, tarfile.TarError):
        return None


def _peer_allowed(addr: str, peers: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr or "")
        return any(ip in ipaddress.ip_network(p.strip(), strict=False) for p in peers.split(",") if p.strip())
    except ValueError:
        return False


_fetch_lock = threading.Lock()


@bp.get("/internal/build-source/<int:dep_id>/<token>.tar.gz")
def build_source(dep_id, token):
    """The build server's one way to read an uploaded project. Anything wrong is a plain 404."""
    cfg = current_app.config
    nothing = current_app.response_class("Not found", status=404, mimetype="text/plain")
    if not cfg.get("BUILD_SOURCE_URL") or "X-Forwarded-For" in request.headers or "Forwarded" in request.headers:
        return nothing
    if cfg.get("BUILD_SOURCE_PEERS") and not _peer_allowed(request.remote_addr, cfg["BUILD_SOURCE_PEERS"]):
        return nothing
    db = get_db()
    dep = db.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    if (dep is None or dep["trigger"] != "upload" or dep["provider"] != engine.COOLIFY
            or dep["status"] not in ("queued", "building")):
        return nothing
    try:
        begun = datetime.fromisoformat(dep["started_at"] or dep["created_at"])
    except (TypeError, ValueError):
        return nothing
    if datetime.now(timezone.utc) - begun > timedelta(seconds=engine.BUILD_TIMEOUT):
        return nothing
    m = re.fullmatch(r"([A-Za-z0-9_-]{43})(?:-([1-9][0-9]?))?", token)
    if m is None:
        return nothing
    token, n = m.group(1), int(m.group(2) or 0)
    tar_path, sha_path, count_path = _source_files(cfg, dep["project_id"], dep_id, n)
    try:
        digest = sha_path.read_text().strip()
    except OSError:
        return nothing
    if not tar_path.is_file() or not hmac.compare_digest(token, build_source_token(cfg, dep_id, digest, n)):
        return nothing
    with _fetch_lock:
        try:
            count = int(count_path.read_text() or 0) + 1
        except (OSError, ValueError):
            count = 1
        if count > SOURCE_FETCH_LIMIT:
            return nothing
        count_path.write_text(str(count))
    if count == 1 and n == 0:
        db.execute("INSERT INTO deploy_logs (deployment_id, ts, level, stage, line) VALUES (?,?,?,?,?)",
                   (dep_id, utcnow(), "info", "building", "The build server received the uploaded source."))
        db.commit()
    resp = send_file(tar_path, mimetype="application/gzip", conditional=False, max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    # An application (not a finished site) goes to the deployment engine, built like a repository.
    plan = source_plan(tmp, project["root_dir"] or "") if source_builds_enabled(db, cfg) else None
    provider, note = engine.STATIC, ""
    if plan is not None:
        from .gitinfo import FRAMEWORKS
        provider = engine.COOLIFY
        detected = plan["framework"]
        if plan.get("root_dir") and not project["root_dir"]:
            db.execute("UPDATE projects SET root_dir = ? WHERE id = ?", (plan["root_dir"], project["id"]))
            project = db.execute("SELECT * FROM projects WHERE id = ?", (project["id"],)).fetchone()
        current = FRAMEWORKS.get(project["framework"], FRAMEWORKS["static"])
        customised = any(project[k] for k in ("install_command", "build_command", "output_dir", "start_command")) \
            or (project["port"] and int(project["port"]) != int(current["port"]))
        if not customised or framework_family(project["framework"]) != framework_family(detected):
            # Untouched settings follow the upload; a different kind of project starts from its preset.
            db.execute("UPDATE projects SET framework = ?, port = ?, install_command = '', build_command = '', "
                       "output_dir = '', start_command = '' WHERE id = ?",
                       (detected, FRAMEWORKS[detected]["port"], project["id"]))
            project = db.execute("SELECT * FROM projects WHERE id = ?", (project["id"],)).fetchone()
        note = f"Detected a {FRAMEWORKS[detected]['name']} project"
        if plan.get("root_dir"):
            note += f" in {plan['root_dir']}/ (Root directory set to {project['root_dir'] + '/' if project['root_dir'] else '.'}; change it in Settings)"
        if project["framework"] != detected:
            note += f"; building it with your {FRAMEWORKS[project['framework']]['name']} settings"
        note += "."
    number = db.execute("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM deployments WHERE project_id = ?",
                        (project["id"],)).fetchone()["n"]
    cur = db.execute(
        "INSERT INTO deployments (project_id, number, status, stage, target, branch, commit_sha, commit_message, "
        "trigger, triggered_by, provider, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (project["id"], number, "queued", "queued", "production", "upload", digest,
         f"Uploaded {os.path.basename(filename)[:80]} ({_human(size)})", "upload", user_id, provider, utcnow()))
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
    if note:
        db.execute("INSERT INTO deploy_logs (deployment_id, ts, level, stage, line) VALUES (?,?,?,?,?)",
                   (dep_id, utcnow(), "info", "queued", note))
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
    limit = plans.project_limit(db, user)
    if user["role"] != "admin":
        try:
            plans.ensure_can_create(db, user)
        except PlanBlocked as exc:
            return err(exc.message, 402)
        count = db.execute("SELECT COUNT(*) AS n FROM projects WHERE owner_id = ?", (user["id"],)).fetchone()["n"]
        if count >= limit:
            return err(f"Your {user['plan'].title()} plan allows {limit} project{'s' if limit != 1 else ''}. "
                       "Delete one or upgrade.", 402)
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
    """Upload a new version. Free projects keep their original 24-hour expiry, and cannot be
    uploaded again once it has passed."""
    p = _load_project(slug)
    if p is None:
        return err("Project not found.", 404)
    if p["source_type"] != "upload":
        return err("This project deploys from a Git repository. Push to the repository to update it.", 409)
    if p["is_paused"]:
        return err("This project is paused. Resume it in Settings to upload.", 409)
    user, db = current_user(), get_db()
    try:
        plans.ensure_can_deploy(db, p)   # a free project cannot be uploaded again once its 24 hours end
    except PlanBlocked as exc:
        return err(exc.message, 402)
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
