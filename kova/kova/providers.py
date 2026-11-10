"""GitHub and GitLab OAuth, repository browsing, and webhook registration.

Design rules this module exists to enforce:

* The OAuth **client secret and the user's access token never reach the browser.**
  The frontend only ever sees a username, an avatar-less display name and a list
  of repositories; token material stays in the database, encrypted at rest.
* The OAuth ``state`` is a single-use nonce kept in the server-side session, so a
  forged callback cannot bind someone else's account.
* Nothing here is a stub: with the client id/secret configured, these are the real
  provider endpoints.
"""
import secrets
import time
from urllib.parse import quote, urlencode

import requests
from flask import Blueprint, current_app, jsonify, redirect, request, session

from .auth import current_user, login_required
from .crypto import EncryptionError, secrets_for
from .db import audit, get_db, utcnow
from .ratelimit import rate_limit

bp = Blueprint("providers", __name__, url_prefix="/api/git")

PROVIDERS = {
    "github": {
        "label": "GitHub",
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        # Least privilege: profile + public repositories/branches. Private repositories are
        # read through the read-only codeskys-deploy GitHub App, never through this token.
        # GitHub's "repo" scope would grant WRITE access to every private repository; it is
        # only used if the owner explicitly sets KOVA_GITHUB_OAUTH_SCOPE.
        "scope": "read:user",
        "docs": "https://github.com/settings/developers",
    },
    "gitlab": {
        "label": "GitLab",
        "authorize": "https://gitlab.com/oauth/authorize",
        "token": "https://gitlab.com/oauth/token",
        "scope": "read_user read_api read_repository",
        "docs": "https://gitlab.com/-/profile/applications",
    },
}


def err(message, code=400):
    return jsonify(error=message), code


def oauth_config(provider):
    cfg = current_app.config
    return (cfg.get(f"{provider.upper()}_CLIENT_ID", ""), cfg.get(f"{provider.upper()}_CLIENT_SECRET", ""))


def configured(provider) -> bool:
    cid, csecret = oauth_config(provider)
    return bool(cid and csecret)


def redirect_uri(provider):
    base = current_app.config.get("PUBLIC_URL") or request.host_url.rstrip("/")
    return f"{base.rstrip('/')}/api/git/{provider}/callback"


# ---------------------------------------------------------------- storage

def oauth_scope(provider: str) -> str:
    if provider == "github":
        override = (current_app.config.get("GITHUB_OAUTH_SCOPE") or "").strip()
        if override:
            return override
    return PROVIDERS[provider]["scope"]


def store_account(conn, user_id, provider, profile, token, refresh=None, expires_in=None, scopes=""):
    box = secrets_for(current_app)
    now = utcnow()
    expires_at = None
    if expires_in:
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + int(expires_in)))
    conn.execute(
        "INSERT INTO git_accounts (user_id, provider, external_id, username, display_name, host, scopes, "
        "token_cipher, refresh_cipher, token_expires_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, provider) DO UPDATE SET external_id=excluded.external_id, "
        "username=excluded.username, display_name=excluded.display_name, scopes=excluded.scopes, "
        "token_cipher=excluded.token_cipher, refresh_cipher=excluded.refresh_cipher, "
        "token_expires_at=excluded.token_expires_at, updated_at=excluded.updated_at",
        (user_id, provider, str(profile.get("id", "")), profile.get("username", ""), profile.get("name", "") or "",
         profile.get("host", ""), scopes, box.encrypt(token),
         box.encrypt(refresh) if refresh else None, expires_at, now, now))
    conn.commit()


def account_for(conn, user_id, provider):
    return conn.execute("SELECT * FROM git_accounts WHERE user_id = ? AND provider = ?",
                        (user_id, provider)).fetchone()


def token_for(conn, account_row):
    """Decrypt an account's access token, refreshing it first when GitLab expired it."""
    if account_row is None:
        return None
    box = secrets_for(current_app)
    try:
        token = box.decrypt(account_row["token_cipher"])
    except EncryptionError:
        return None
    if account_row["provider"] == "gitlab" and account_row["token_expires_at"]:
        if account_row["token_expires_at"] <= utcnow() and account_row["refresh_cipher"]:
            try:
                token = _refresh_gitlab(conn, account_row, box.decrypt(account_row["refresh_cipher"]))
            except (EncryptionError, requests.RequestException):
                return None
    return token


def _refresh_gitlab(conn, account_row, refresh_token):
    cid, csecret = oauth_config("gitlab")
    r = requests.post(PROVIDERS["gitlab"]["token"], timeout=15, headers={"Accept": "application/json"}, data={
        "client_id": cid, "client_secret": csecret, "refresh_token": refresh_token,
        "grant_type": "refresh_token", "redirect_uri": redirect_uri("gitlab")})
    r.raise_for_status()
    data = r.json()
    box = secrets_for(current_app)
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                               time.gmtime(time.time() + int(data.get("expires_in") or 7200)))
    conn.execute("UPDATE git_accounts SET token_cipher=?, refresh_cipher=?, token_expires_at=?, updated_at=? "
                 "WHERE id = ?",
                 (box.encrypt(data["access_token"]),
                  box.encrypt(data.get("refresh_token") or refresh_token), expires_at, utcnow(),
                  account_row["id"]))
    conn.commit()
    return data["access_token"]


def token_for_project(conn, project):
    """The token a deploy/webhook path should use for this project, if any."""
    if not project["git_account_id"]:
        return None
    row = conn.execute("SELECT * FROM git_accounts WHERE id = ?", (project["git_account_id"],)).fetchone()
    return token_for(conn, row)


# ---------------------------------------------------------------- OAuth flow

@bp.get("/providers")
@login_required
def list_providers():
    conn = get_db()
    user = current_user()
    out = []
    for name, meta in PROVIDERS.items():
        acc = account_for(conn, user["id"], name)
        out.append({
            "provider": name, "label": meta["label"], "configured": configured(name),
            "setup_url": meta["docs"], "scope": oauth_scope(name),
            "callback_url": redirect_uri(name),
            "connected": acc is not None,
            "username": acc["username"] if acc else None,
            "connected_at": acc["created_at"] if acc else None,
        })
    return jsonify(providers=out)


@bp.get("/<provider>/authorize")
@login_required
def authorize(provider):
    if provider not in PROVIDERS:
        return err("Unknown provider.", 404)
    if not configured(provider):
        return err(f"{PROVIDERS[provider]['label']} OAuth is not configured on this server. "
                   f"An administrator must set {provider.upper()}_CLIENT_ID and "
                   f"{provider.upper()}_CLIENT_SECRET.", 503)
    nonce = secrets.token_urlsafe(24)
    session[f"oauth_state_{provider}"] = nonce
    cid, _ = oauth_config(provider)
    params = {"client_id": cid, "redirect_uri": redirect_uri(provider),
              "scope": oauth_scope(provider), "state": nonce}
    if provider == "gitlab":
        params["response_type"] = "code"
    return jsonify(url=PROVIDERS[provider]["authorize"] + "?" + urlencode(params))


@bp.get("/<provider>/callback")
@login_required
def callback(provider):
    if provider not in PROVIDERS:
        return err("Unknown provider.", 404)
    limited = rate_limit(get_db(), f"oauth:{provider}:{current_user()['id']}", limit=10, per_seconds=300)
    if limited:
        return err("Too many connection attempts. Wait a few minutes and try again.", 429)
    expected = session.pop(f"oauth_state_{provider}", None)
    supplied = request.args.get("state", "")
    if not expected or not secrets.compare_digest(expected, supplied):
        return err("The authorization request could not be verified. Start the connection again.", 400)
    if request.args.get("error"):
        return redirect("/#/settings?s=git&error=" + quote(request.args.get("error_description") or
                                                           request.args["error"]))
    code = request.args.get("code", "")
    if not code:
        return err("No authorization code was returned.", 400)
    cid, csecret = oauth_config(provider)
    try:
        if provider == "github":
            token, scopes, refresh, expires = _exchange_github(cid, csecret, code)
            profile = _github_profile(token)
        else:
            token, scopes, refresh, expires = _exchange_gitlab(cid, csecret, code)
            profile = _gitlab_profile(token)
    except requests.RequestException as exc:
        return err(f"Could not reach {PROVIDERS[provider]['label']}: {exc.__class__.__name__}", 502)
    except ValueError as exc:
        return err(str(exc), 400)
    conn = get_db()
    store_account(conn, current_user()["id"], provider, profile, token, refresh, expires, scopes)
    audit("git.connected", actor_id=current_user()["id"], target=f"{provider}:{profile.get('username')}",
          ip=request.remote_addr)
    return redirect("/#/settings?s=git&connected=" + provider)


def _exchange_github(cid, csecret, code):
    r = requests.post(PROVIDERS["github"]["token"], timeout=15, headers={"Accept": "application/json"},
                      data={"client_id": cid, "client_secret": csecret, "code": code,
                            "redirect_uri": redirect_uri("github")})
    r.raise_for_status()
    data = r.json()
    if data.get("error") or not data.get("access_token"):
        raise ValueError(data.get("error_description") or "GitHub did not return an access token.")
    return data["access_token"], data.get("scope", ""), None, None


def _exchange_gitlab(cid, csecret, code):
    r = requests.post(PROVIDERS["gitlab"]["token"], timeout=15, headers={"Accept": "application/json"},
                      data={"client_id": cid, "client_secret": csecret, "code": code,
                            "grant_type": "authorization_code", "redirect_uri": redirect_uri("gitlab")})
    r.raise_for_status()
    data = r.json()
    if data.get("error") or not data.get("access_token"):
        raise ValueError(data.get("error_description") or "GitLab did not return an access token.")
    return data["access_token"], data.get("scope", ""), data.get("refresh_token"), data.get("expires_in")


def _github_profile(token):
    r = requests.get("https://api.github.com/user", timeout=15, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "Codeskys-Deploy"})
    r.raise_for_status()
    u = r.json()
    return {"id": u.get("id"), "username": u.get("login", ""), "name": u.get("name") or "", "host": "github.com"}


def _gitlab_profile(token):
    r = requests.get("https://gitlab.com/api/v4/user", timeout=15,
                     headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    u = r.json()
    return {"id": u.get("id"), "username": u.get("username", ""), "name": u.get("name") or "", "host": "gitlab.com"}


@bp.delete("/<provider>")
@login_required
def disconnect(provider):
    if provider not in PROVIDERS:
        return err("Unknown provider.", 404)
    conn = get_db()
    user = current_user()
    acc = account_for(conn, user["id"], provider)
    if acc is None:
        return err("That account is not connected.", 404)
    using = conn.execute("SELECT COUNT(*) AS n FROM projects WHERE git_account_id = ?", (acc["id"],)).fetchone()["n"]
    conn.execute("DELETE FROM git_accounts WHERE id = ?", (acc["id"],))
    conn.commit()
    audit("git.disconnected", actor_id=user["id"], target=provider, ip=request.remote_addr)
    return jsonify(ok=True, projects_affected=using)


# ---------------------------------------------------------------- browsing

@bp.get("/<provider>/repos")
@login_required
def repos(provider):
    if provider not in PROVIDERS:
        return err("Unknown provider.", 404)
    conn = get_db()
    acc = account_for(conn, current_user()["id"], provider)
    if acc is None:
        return err(f"Connect your {PROVIDERS[provider]['label']} account first.", 409)
    token = token_for(conn, acc)
    if not token:
        return err(f"The stored {PROVIDERS[provider]['label']} token is no longer usable. Reconnect the account.", 409)
    query = (request.args.get("q") or "").strip()[:100]
    try:
        items = _github_repos(token, query) if provider == "github" else _gitlab_repos(token, query)
    except requests.RequestException as exc:
        return err(f"Could not reach {PROVIDERS[provider]['label']}: {exc.__class__.__name__}", 502)
    return jsonify(repos=items, provider=provider, username=acc["username"])


def _github_repos(token, query):
    r = requests.get("https://api.github.com/user/repos", timeout=20, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "Codeskys-Deploy"},
        params={"per_page": 100, "sort": "updated", "affiliation": "owner,collaborator,organization_member"})
    r.raise_for_status()
    out = []
    for repo in r.json():
        if query and query.lower() not in repo["full_name"].lower():
            continue
        out.append({"external_id": str(repo["id"]), "full_name": repo["full_name"], "name": repo["name"],
                    "url": repo["html_url"], "private": bool(repo["private"]),
                    "default_branch": repo.get("default_branch") or "main",
                    "description": repo.get("description") or "", "language": repo.get("language"),
                    "updated_at": repo.get("pushed_at")})
    return out


def _gitlab_repos(token, query):
    params = {"membership": "true", "per_page": 100, "order_by": "last_activity_at", "simple": "true"}
    if query:
        params["search"] = query
    r = requests.get("https://gitlab.com/api/v4/projects", timeout=20,
                     headers={"Authorization": f"Bearer {token}"}, params=params)
    r.raise_for_status()
    return [{"external_id": str(p["id"]), "full_name": p["path_with_namespace"], "name": p["name"],
             "url": p["web_url"], "private": p.get("visibility") != "public",
             "default_branch": p.get("default_branch") or "main",
             "description": p.get("description") or "", "language": None,
             "updated_at": p.get("last_activity_at")} for p in r.json()]


@bp.get("/<provider>/branches")
@login_required
def branches(provider):
    if provider not in PROVIDERS:
        return err("Unknown provider.", 404)
    full_name = (request.args.get("repo") or "").strip()
    if not full_name or "/" not in full_name:
        return err("Pass the repository as owner/name.")
    conn = get_db()
    acc = account_for(conn, current_user()["id"], provider)
    token = token_for(conn, acc) if acc else None
    try:
        if provider == "github":
            r = requests.get(f"https://api.github.com/repos/{full_name}/branches", timeout=15, headers={
                "Authorization": f"Bearer {token}" if token else "", "Accept": "application/vnd.github+json",
                "User-Agent": "Codeskys-Deploy"}, params={"per_page": 100})
            if r.status_code == 404:
                return err("Repository not found, or the connected account cannot see it.", 404)
            r.raise_for_status()
            names = [b["name"] for b in r.json()]
        else:
            r = requests.get(
                f"https://gitlab.com/api/v4/projects/{quote(full_name, safe='')}/repository/branches",
                timeout=15, headers={"Authorization": f"Bearer {token}"} if token else {},
                params={"per_page": 100})
            if r.status_code == 404:
                return err("Project not found, or the connected account cannot see it.", 404)
            r.raise_for_status()
            names = [b["name"] for b in r.json()]
    except requests.RequestException as exc:
        return err(f"Could not reach {PROVIDERS[provider]['label']}: {exc.__class__.__name__}", 502)
    return jsonify(branches=names)
