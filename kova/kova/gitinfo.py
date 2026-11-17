"""Git URL parsing, repository metadata and framework detection.

Works unauthenticated for public repositories and with a caller-supplied OAuth
token for private ones. Tokens are passed in per call — nothing is cached here.
"""
import base64
import json
import os
import re
from urllib.parse import quote

import requests

GITHUB_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
GITLAB_RE = re.compile(r"^(?:https?://)?(?:www\.)?gitlab\.com/((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+?)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
GENERIC_GIT_RE = re.compile(r"^https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}/[\w./-]+$")
# A folder (or branch) view copied from the browser: github.com/<owner>/<repo>/tree/<ref>[/<dir>].
# The ref may itself contain slashes, so splitting it is left to resolve_tree().
GITHUB_TREE_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)"
                            r"/(tree|blob)/([^?#]+?)/?(?:[?#].*)?$")
# Root directories the deployment engine can build (same rule as composegen.build_context).
ROOT_DIR_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")

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
    "streamlit": {"name": "Python (Streamlit)", "install": "pip install -r requirements.txt", "build": "", "output": "", "start": "streamlit run app.py --server.port 8000 --server.address 0.0.0.0 --server.headless true", "port": 8000, "static": False},
    "fastapi":  {"name": "Python (FastAPI)", "install": "pip install -r requirements.txt", "build": "", "output": "", "start": "uvicorn main:app --host 0.0.0.0 --port 8000", "port": 8000, "static": False},
    "django":   {"name": "Python (Django)", "install": "pip install -r requirements.txt", "build": "python manage.py collectstatic --noinput", "output": "", "start": "gunicorn config.wsgi -b 0.0.0.0:8000", "port": 8000, "static": False},
    "gradio":   {"name": "Python (Gradio)", "install": "pip install -r requirements.txt", "build": "", "output": "", "start": "python app.py", "port": 7860, "static": False},
    "dash":     {"name": "Python (Plotly Dash)", "install": "pip install -r requirements.txt", "build": "", "output": "", "start": "python app.py", "port": 8050, "static": False},
    "nestjs":   {"name": "NestJS",          "install": "npm install", "build": "npm run build", "output": "",        "start": "node dist/main", "port": 3000, "static": False},
    "docker":   {"name": "Dockerfile",      "install": "",            "build": "",              "output": "",        "start": "",            "port": 3000, "static": False},
    # The repository's own docker-compose file: several services (web, API, worker, database).
    "compose":  {"name": "Docker Compose (multi-service)", "install": "", "build": "", "output": "",   "start": "",            "port": 3000, "static": False},
    # No web application: a read-only page of the project's notebooks and README.
    "docs":     {"name": "Notebook & docs preview", "install": "", "build": "",          "output": "",        "start": "",            "port": 80,   "static": True},
    "static":   {"name": "Static HTML",     "install": "",            "build": "",              "output": "",        "start": "",            "port": 80,   "static": True},
}


class GitError(ValueError):
    pass


def parse_repo(url: str) -> dict:
    url = (url or "").strip()
    m = GITHUB_TREE_RE.match(url)
    if m:
        owner, repo, kind, ref_path = m.groups()
        if kind == "blob":
            raise GitError("That link points to a single file. Use the repository link "
                           "(https://github.com/owner/repo) or a folder link (…/tree/<branch>/<folder>).")
        parts = [p for p in ref_path.split("/") if p]
        if not parts or any(p in (".", "..") for p in parts):
            raise GitError("That GitHub folder link is not valid.")
        return {"provider": "github", "owner": owner, "repo": repo, "full_name": f"{owner}/{repo}",
                "url": f"https://github.com/{owner}/{repo}", "tree_path": "/".join(parts)}
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


def clean_root_dir(value) -> str:
    """A project root directory the deployment engine accepts, or GitError."""
    root = str(value or "").strip().strip("/")
    if root in ("", "."):
        return ""
    root = root.removeprefix("./")
    if not ROOT_DIR_RE.match(root) or ".." in root.split("/") or len(root) > 200:
        raise GitError("The root directory may use letters, digits, '.', '_', '-' and '/' between folders "
                       "(for example apps/web).")
    return root


def resolve_tree(info: dict, timeout: float = 6.0, token: str | None = None, lookups: bool = True):
    """Split a ``/tree/<ref>/<dir>`` path into ``(branch, root_dir)``.

    Branch names may contain slashes (``feature/login``), so the path is matched against
    the repository's real branches, longest first. Without GitHub lookups only an
    unambiguous single-segment path (``/tree/main``) is accepted."""
    parts = info["tree_path"].split("/")
    if not lookups:
        if len(parts) == 1:
            return parts[0], ""
        raise GitError("Codeskys could not check that folder link. Enter the repository link "
                       "(https://github.com/owner/repo), then choose the branch and root directory.")
    refs = smart_refs(info["full_name"], max(timeout, 10.0)) if info.get("provider") == "github" else None
    if refs:                                    # a public repository: every branch in one request, no API
        for i in range(len(parts), 0, -1):
            if f"refs/heads/{'/'.join(parts[:i])}" in refs:
                return "/".join(parts[:i]), clean_root_dir("/".join(parts[i:]))
    try:
        for i in range(len(parts), 0, -1):
            branch = "/".join(parts[:i])
            r = _gh(f"/repos/{info['full_name']}/branches/{quote(branch, safe='/')}", timeout, token)
            if r.status_code == 200:
                return branch, clean_root_dir("/".join(parts[i:]))
            if r.status_code not in (404, 422):
                raise GitError("GitHub did not answer while checking that folder link. Try again, or enter "
                               "the repository link and choose the branch and root directory.")
    except requests.RequestException:
        raise GitError("GitHub could not be reached to check that folder link. Try again in a moment.") from None
    raise GitError(f"No branch of {info['full_name']} matches that link. Check the branch name, or connect "
                   "the GitHub account that can see the repository.")


def _requirement_names(requirements: str) -> set:
    """Lower-case package names listed in a requirements.txt (versions, extras, comments dropped)."""
    names = set()
    for line in (requirements or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            names.add(re.split(r"[\s\[<>=!~;@]", line, maxsplit=1)[0].lower().replace("_", "-"))
    return names


def detect_framework(pkg: dict | None, files: set, requirements: str = "") -> str:
    if "Dockerfile" in files:
        return "docker"
    if pkg:
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        for dep, fw in (("next", "nextjs"), ("nuxt", "nuxt"), ("@sveltejs/kit", "svelte"), ("@nestjs/core", "nestjs"),
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
    reqs = _requirement_names(requirements)
    if "fastapi" in reqs and "flask" not in reqs:
        return "fastapi"
    if "streamlit" in reqs and "flask" not in reqs:
        return "streamlit"
    if "gradio" in reqs and "flask" not in reqs:
        return "gradio"
    if "dash" in reqs:
        return "dash"           # Dash runs on Flask, so requirements often list both
    if "requirements.txt" in files or "app.py" in files:
        return "flask"
    return "static"


# ---------------------------------------------------------------- GitHub

def api_token(token: str | None = None) -> str | None:
    """The caller's token, else the platform's optional read-only token for public repositories
    (KOVA_GITHUB_API_TOKEN: 5,000 GitHub API calls an hour instead of 60 for the whole server)."""
    return token or os.environ.get("KOVA_GITHUB_API_TOKEN", "").strip() or None


def _gh(path: str, timeout: float, token: str | None = None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Codeskys-Deploy"}
    token = api_token(token)
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
              "private": False, "stars": 0, "language": None, "latest_commit": None, "reachable": None,
              "root_dir": ""}
    if info.get("tree_path"):
        # A folder link: the branch and root directory come from the link itself.
        branch, root = resolve_tree(info, timeout, token)
        result.update(default_branch=branch, branches=[branch], root_dir=root)
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
        # Usually the API's hourly limit for this server (403/429): detect without the API.
        return _inspect_github_raw(info, result, timeout)
    meta = r.json()
    linked = info.get("tree_path")
    result.update(default_branch=result["default_branch"] if linked else (meta.get("default_branch") or "main"),
                  description=meta.get("description") or "",
                  stars=meta.get("stargazers_count", 0), language=meta.get("language"),
                  private=bool(meta.get("private")), reachable=True,
                  external_id=str(meta.get("id") or ""))
    br = _gh(f"/repos/{info['full_name']}/branches?per_page=100", timeout, token)
    if br.status_code == 200:
        result["branches"] = [b["name"] for b in br.json()] or [result["default_branch"]]
        if result["default_branch"] not in result["branches"]:
            result["branches"].insert(0, result["default_branch"])
    ref = quote(result["default_branch"], safe="")
    base = f"/repos/{info['full_name']}/contents" + (f"/{result['root_dir']}" if result["root_dir"] else "")
    tree = _gh(f"{base}?ref={ref}", timeout, token)
    if tree.status_code in (403, 429) and not result["private"]:
        # The hourly API limit ran out between two calls: finish the detection without the API.
        return _inspect_github_raw(info, result, timeout, branch=result["default_branch"])
    files = set()
    if tree.status_code == 200 and isinstance(tree.json(), list):
        files = {f["name"] for f in tree.json()}
    pkg = None
    if "package.json" in files:
        pr = _gh(f"{base}/package.json?ref={ref}", timeout, token)
        if pr.status_code == 200:
            try:
                pkg = json.loads(base64.b64decode(pr.json().get("content", "")).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                pkg = None
    reqs = ""
    if "requirements.txt" in files and "package.json" not in files:
        rr = _gh(f"{base}/requirements.txt?ref={ref}", timeout, token)
        if rr.status_code == 200:
            try:
                reqs = base64.b64decode(rr.json().get("content", "")).decode("utf-8", "replace")[:20000]
            except ValueError:
                reqs = ""
    from .projectscan import choose
    cache = {"package.json": json.dumps(pkg) if pkg is not None else None, "requirements.txt": reqs or None}

    def read(name):
        if name not in cache:
            cache[name] = None
            if name in files:
                fr = _gh(f"{base}/{name}?ref={ref}", timeout, token)
                if fr.status_code == 200:
                    try:
                        cache[name] = base64.b64decode(fr.json().get("content", "")).decode("utf-8", "replace")[:65536]
                    except ValueError:
                        pass
        return cache[name]
    # Docker Compose with several services, a notebook/README-only project, or one framework.
    result["framework"] = choose(files, read) or "static"
    if result["framework"] in ("docs", "static") and not result["private"]:
        _detect_in_folder(info, result)
    result["files"] = sorted(files)[:40]
    cm = _gh(f"/repos/{info['full_name']}/commits/{quote(result['default_branch'], safe='/')}", timeout, token)
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
        r = _gh(f"/repos/{full_name}/commits/{quote(branch, safe='/')}", timeout, token)
        if r.status_code == 200:
            c = r.json()
            return {"sha": c["sha"], "message": c["commit"]["message"].splitlines()[0][:200]}
    except requests.RequestException:
        pass
    if provider == "github":
        # The REST API is limited (60 calls an hour per server without a token) or may be down;
        # git's own protocol still says where the branch points, without using that budget.
        sha = branch_head(full_name, branch, timeout)
        if sha:
            return {"sha": sha, "message": ""}
    return None


# Files that decide the framework, probed one by one on raw.githubusercontent.com.
RAW_PROBES = ("package.json", "requirements.txt", "Dockerfile", "manage.py", "pyproject.toml", "docker-compose.yml",
              "docker-compose.yaml", "compose.yml", "compose.yaml", "index.html", "app.py", "main.py",
              "streamlit_app.py", "README.md")


def default_branch(full_name: str, timeout: float = 10.0) -> str | None:
    """A public GitHub repository's default branch from the smart-HTTP handshake (no API call)."""
    return (smart_refs(full_name, timeout) or {}).get("HEAD->")


def _inspect_github_raw(info, result, timeout, branch=None):
    """Detection when the GitHub API is unavailable: the marker files are read from
    raw.githubusercontent.com, which has no hourly limit. Only public repositories."""
    from concurrent.futures import ThreadPoolExecutor

    from .projectscan import choose
    if not branch:
        branch = result["default_branch"] if info.get("tree_path") else default_branch(info["full_name"], timeout)
    if not branch:
        result["reachable"] = False
        return result
    result.update(default_branch=branch, branches=[branch], reachable=True)
    prefix = f"{result['root_dir']}/" if result["root_dir"] else ""

    def get(name):
        url = f"https://raw.githubusercontent.com/{info['full_name']}/{quote(branch, safe='/')}/{prefix}{name}"
        try:
            r = requests.get(url, timeout=timeout, allow_redirects=False)
        except requests.RequestException:
            return name, None
        return name, (r.text[:65536] if r.status_code == 200 else None)
    with ThreadPoolExecutor(8) as pool:
        found = {n: t for n, t in pool.map(get, RAW_PROBES) if t is not None}
    result["framework"] = choose(set(found), found.get) or "static"
    if result["framework"] in ("docs", "static"):
        _detect_in_folder(info, result)
    result["files"] = sorted(found)
    result["detected_without_api"] = True
    return result


def smart_refs(full_name: str, timeout: float = 10.0) -> dict | None:
    """``{ref: sha}`` of a public GitHub repository, read like ``git ls-remote`` (smart HTTP, no API
    call); the key ``"HEAD->"`` holds the default branch's name. None when it cannot be read."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name or ""):
        return None
    try:
        r = requests.get(f"https://github.com/{full_name}.git/info/refs", params={"service": "git-upload-pack"},
                         headers={"User-Agent": "git/2.43 Codeskys-Deploy"}, timeout=timeout,
                         allow_redirects=False)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    data, pos, refs = r.content[:2_000_000], 0, {}
    while pos + 4 <= len(data):
        try:
            size = int(data[pos:pos + 4], 16)
        except ValueError:
            return None
        if size == 0:                       # flush packet
            pos += 4
            continue
        line, _, caps = data[pos + 4:pos + size].rstrip(b"\n").partition(b"\0")
        pos += size
        head = re.search(rb"symref=HEAD:refs/heads/(\S{1,200})", caps)
        if head:
            refs["HEAD->"] = head.group(1).decode("utf-8", "replace")
        sha, _, ref = line.partition(b" ")
        if re.fullmatch(rb"[0-9a-f]{40}", sha):
            refs[ref.decode("utf-8", "replace")] = sha.decode()
    return refs


def _detect_in_folder(info, result):
    """Only docs (a README, notebooks) or nothing at the top: an app one or two folders down (forgeops/
    holding a compose stack) is what the student deploys, so the form proposes it and its folder.
    One file-list request (the archive when the API limit is used up); public repositories only."""
    from .buildpolicy import fetch_file, repository_files
    from .projectscan import _has_site, detect
    branch = result["default_branch"]
    sha = branch_head(info["full_name"], branch)
    url = f"https://github.com/{info['full_name']}"
    every = repository_files(url, sha) if sha else None
    if not every:
        return
    root = result["root_dir"].strip("/")
    files = {f[len(root) + 1:] if root else f for f in every if not root or f.startswith(root + "/")}
    if result["framework"] == "static" and _has_site(files):
        return                                                 # a real static site
    try:
        framework, sub = detect(files, lambda p: fetch_file(url, sha, f"{root}/{p}" if root else p))
    except Exception:                                          # detection only proposes; never block the form
        return
    if framework and sub:
        result.update(framework=framework, root_dir="/".join(x for x in (root, sub) if x))


def branch_head(full_name: str, branch: str, timeout: float = 10.0) -> str | None:
    """The commit a public GitHub branch points to, read like ``git ls-remote`` (smart HTTP)."""
    if not branch:
        return None
    return (smart_refs(full_name, timeout) or {}).get(f"refs/heads/{branch}")
