"""Git URL parsing, repository metadata and framework detection.

Works unauthenticated for public repositories and with a caller-supplied OAuth
token for private ones. Tokens are passed in per call — nothing is cached here.
"""
import base64
import json
import re
from urllib.parse import quote

import requests

GITHUB_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
GITLAB_RE = re.compile(r"^(?:https?://)?(?:www\.)?gitlab\.com/((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+?)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
GENERIC_GIT_RE = re.compile(r"^https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}/[\w./-]+$")

# Build presets, modelled on what Netlify / Vercel / Coolify detect.
FRAMEWORKS = {
    "nextjs":   {"name": "Next.js",         "install": "npm install", "build": "npm run build", "output": ".next",   "start": "npm start",   "port": 3000, "static": False},
    "react":    {"name": "React (Vite)",    "install": "npm install", "build": "npm run build", "output": "dist",    "start": "",            "port": 80,   "static": True},
    "cra":      {"name": "Create React App", "install": "npm install", "build": "npm run build", "output": "build",  "start": "",            "port": 80,   "static": True},
    "vue":      {"name": "Vue",             "install": "npm install", "build": "npm run build", "output": "dist",    "start": "",            "port": 80,   "static": True},
    "nuxt":     {"name": "Nuxt",            "install": "npm install", "build": "npm run build", "output": ".output", "start": "node .output/server/index.mjs", "port": 3000, "static": False},
    "svelte":   {"name": "SvelteKit",       "install": "npm install", "build": "npm run build", "output": "build",   "start": "node build",  "port": 3000, "static": False},
    "angular":  {"name": "Angular",         "install": "npm install", "build": "npm run build", "output": "dist",    "start": "",            "port": 80,   "static": True},
    "astro":    {"name": "Astro",           "install": "npm install", "build": "npm run build", "output": "dist",    "start": "",            "port": 80,   "static": True},
    "gatsby":   {"name": "Gatsby",          "install": "npm install", "build": "npm run build", "output": "public",  "start": "",            "port": 80,   "static": True},
    "node":     {"name": "Node.js / Express", "install": "npm install", "build": "",            "output": "",        "start": "npm start",   "port": 3000, "static": False},
    "flask":    {"name": "Python (Flask)",  "install": "pip install -r requirements.txt", "build": "", "output": "", "start": "gunicorn app:app -b 0.0.0.0:8000", "port": 8000, "static": False},
    "django":   {"name": "Python (Django)", "install": "pip install -r requirements.txt", "build": "python manage.py collectstatic --noinput", "output": "", "start": "gunicorn config.wsgi -b 0.0.0.0:8000", "port": 8000, "static": False},
    "docker":   {"name": "Dockerfile",      "install": "",            "build": "",              "output": "",        "start": "",            "port": 3000, "static": False},
    "static":   {"name": "Static HTML",     "install": "",            "build": "",              "output": "",        "start": "",            "port": 80,   "static": True},
}


class GitError(ValueError):
    pass


def parse_repo(url: str) -> dict:
    url = (url or "").strip()
    m = GITHUB_RE.match(url)
    if m:
        owner, repo = m.group(1), m.group(2)
        return {"provider": "github", "owner": owner, "repo": repo, "full_name": f"{owner}/{repo}",
                "url": f"https://github.com/{owner}/{repo}"}
    m = GITLAB_RE.match(url)
    if m:
        namespace, repo = m.group(1), m.group(2)
        return {"provider": "gitlab", "owner": namespace, "repo": repo, "full_name": f"{namespace}/{repo}",
                "url": f"https://gitlab.com/{namespace}/{repo}"}
    if GENERIC_GIT_RE.match(url):
        name = url.rstrip("/").split("/")[-1].removesuffix(".git")
        return {"provider": "git", "owner": "", "repo": name, "full_name": name, "url": url}
    raise GitError("Enter a public Git repository URL, for example https://github.com/owner/repo")


def detect_framework(pkg: dict | None, files: set) -> str:
    if "Dockerfile" in files:
        return "docker"
    if pkg:
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        for dep, fw in (("next", "nextjs"), ("nuxt", "nuxt"), ("@sveltejs/kit", "svelte"),
                        ("@angular/core", "angular"), ("astro", "astro"), ("gatsby", "gatsby"),
                        ("react-scripts", "cra")):
            if dep in deps:
                return fw
        if "vue" in deps:
            return "vue"
        if "react" in deps:
            return "react"
        if "express" in deps or "fastify" in deps or "koa" in deps or pkg.get("scripts", {}).get("start"):
            return "node"
        if "vite" in deps:
            return "react"
    if "manage.py" in files:
        return "django"
    if "requirements.txt" in files or "app.py" in files:
        return "flask"
    return "static"


# ---------------------------------------------------------------- GitHub

def _gh(path: str, timeout: float, token: str | None = None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Codeskys-Deploy"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.get("https://api.github.com" + path, timeout=timeout, headers=headers)


def _gl(path: str, timeout: float, token: str | None = None, params=None):
    headers = {"User-Agent": "Codeskys-Deploy"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.get("https://gitlab.com/api/v4" + path, timeout=timeout, headers=headers, params=params or {})


def inspect_repo(url: str, timeout: float = 8.0, token: str | None = None, provider: str | None = None) -> dict:
    """Look a repository up. Network failures degrade to a parse-only result."""
    info = parse_repo(url)
    kind = provider or info["provider"]
    result = {**info, "default_branch": "main", "branches": ["main"], "description": "", "framework": "static",
              "private": False, "stars": 0, "language": None, "latest_commit": None, "reachable": None}
    try:
        if kind == "github":
            return _inspect_github(info, result, timeout, token)
        if kind == "gitlab":
            return _inspect_gitlab(info, result, timeout, token)
    except requests.RequestException:
        result["reachable"] = False
    return result


def _inspect_github(info, result, timeout, token):
    r = _gh(f"/repos/{info['full_name']}", timeout, token)
    if r.status_code == 404:
        raise GitError("Repository not found. Check the URL, or connect the GitHub account that can see it.")
    if r.status_code == 401:
        raise GitError("GitHub rejected the stored token. Reconnect your GitHub account.")
    if r.status_code != 200:
        result["reachable"] = False
        return result
    meta = r.json()
    result.update(default_branch=meta.get("default_branch") or "main", description=meta.get("description") or "",
                  stars=meta.get("stargazers_count", 0), language=meta.get("language"),
                  private=bool(meta.get("private")), reachable=True,
                  external_id=str(meta.get("id") or ""))
    br = _gh(f"/repos/{info['full_name']}/branches?per_page=100", timeout, token)
    if br.status_code == 200:
        result["branches"] = [b["name"] for b in br.json()] or [result["default_branch"]]
    tree = _gh(f"/repos/{info['full_name']}/contents?ref={result['default_branch']}", timeout, token)
    files = set()
    if tree.status_code == 200 and isinstance(tree.json(), list):
        files = {f["name"] for f in tree.json()}
    pkg = None
    if "package.json" in files:
        pr = _gh(f"/repos/{info['full_name']}/contents/package.json?ref={result['default_branch']}", timeout, token)
        if pr.status_code == 200:
            try:
                pkg = json.loads(base64.b64decode(pr.json().get("content", "")).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                pkg = None
    result["framework"] = detect_framework(pkg, files)
    result["files"] = sorted(files)[:40]
    cm = _gh(f"/repos/{info['full_name']}/commits/{result['default_branch']}", timeout, token)
    if cm.status_code == 200:
        c = cm.json()
        result["latest_commit"] = {"sha": c["sha"], "message": c["commit"]["message"].splitlines()[0][:200],
                                   "author": c["commit"]["author"]["name"]}
    return result


def _inspect_gitlab(info, result, timeout, token):
    pid = quote(info["full_name"], safe="")
    r = _gl(f"/projects/{pid}", timeout, token)
    if r.status_code == 404:
        raise GitError("Project not found. Check the URL, or connect the GitLab account that can see it.")
    if r.status_code == 401:
        raise GitError("GitLab rejected the stored token. Reconnect your GitLab account.")
    if r.status_code != 200:
        result["reachable"] = False
        return result
    meta = r.json()
    result.update(default_branch=meta.get("default_branch") or "main", description=meta.get("description") or "",
                  stars=meta.get("star_count", 0), private=meta.get("visibility") != "public",
                  reachable=True, external_id=str(meta.get("id") or ""))
    br = _gl(f"/projects/{pid}/repository/branches", timeout, token, {"per_page": 100})
    if br.status_code == 200:
        result["branches"] = [b["name"] for b in br.json()] or [result["default_branch"]]
    tree = _gl(f"/projects/{pid}/repository/tree", timeout, token,
               {"ref": result["default_branch"], "per_page": 100})
    files = {f["name"] for f in tree.json()} if tree.status_code == 200 and isinstance(tree.json(), list) else set()
    pkg = None
    if "package.json" in files:
        pr = _gl(f"/projects/{pid}/repository/files/package.json", timeout, token,
                 {"ref": result["default_branch"]})
        if pr.status_code == 200:
            try:
                pkg = json.loads(base64.b64decode(pr.json().get("content", "")).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                pkg = None
    result["framework"] = detect_framework(pkg, files)
    result["files"] = sorted(files)[:40]
    cm = _gl(f"/projects/{pid}/repository/commits", timeout, token,
             {"ref_name": result["default_branch"], "per_page": 1})
    if cm.status_code == 200 and cm.json():
        c = cm.json()[0]
        result["latest_commit"] = {"sha": c["id"], "message": (c.get("title") or "")[:200],
                                   "author": c.get("author_name", "")}
    return result


def latest_commit(full_name: str, branch: str, timeout: float = 6.0, token: str | None = None,
                  provider: str = "github"):
    if not full_name or "/" not in full_name:
        return None
    try:
        if provider == "gitlab":
            r = _gl(f"/projects/{quote(full_name, safe='')}/repository/commits", timeout, token,
                    {"ref_name": branch, "per_page": 1})
            if r.status_code == 200 and r.json():
                c = r.json()[0]
                return {"sha": c["id"], "message": (c.get("title") or "")[:200]}
            return None
        r = _gh(f"/repos/{full_name}/commits/{branch}", timeout, token)
        if r.status_code == 200:
            c = r.json()
            return {"sha": c["sha"], "message": c["commit"]["message"].splitlines()[0][:200]}
    except requests.RequestException:
        pass
    return None
