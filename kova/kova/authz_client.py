"""Server-side client for the Codeskys authorization service (ForgeOps ``authz``).

Contract: ``forgeops/authz/WEBSITE_INTEGRATION.md`` (the source of truth).

* Base URL ``CODESKYS_AUTHZ_URL`` (``http://authz:8010`` on the private ``control``
  network shared by the website and authz only).
* Auth header ``X-Website-Key`` (``CODESKYS_AUTHZ_WEBSITE_KEY`` or ``..._FILE``).
  The key opens ``/internal/website/*`` and nothing else. It is never sent to a
  browser, logged, or included in an exception.
* Every request sends ``X-Authz-Errors: v2``.

Error shapes accepted (all normalised into :class:`AuthzError`): the v2 envelope
``{"ok": false, "error": {code, reference, message}, "detail": ...}``, FastAPI's
``{"detail": ...}``, and legacy ``200 {"ok": false, "reason": ...}``.

The ``runtime_secret`` returned by :meth:`AuthzClient.create_deployment` is shown
exactly once. Callers must hand it straight to the runtime-secret sink and drop it:
never the website DB, logs, a build env var or the student app.
"""
from __future__ import annotations

import re

import requests

AUTH = "ERR_EXEC_CONTEXT_0x71C9"
CRED = "ERR_RUNTIME_STATE_0x83A2"
REPO = "ERR_RESOURCE_BINDING_0x92D4"
REPLAY = "ERR_SESSION_VALIDATION_0x64B8"
POLICY = "ERR_DEPLOY_GATE_0x51F7"
NOT_FOUND = "ERR_NOT_FOUND"
INVALID = "ERR_REQUEST_INVALID"
UNAVAILABLE = "ERR_SERVICE_UNAVAILABLE"

# Fallbacks when authz sends no message (or an old server's opaque one).
_FALLBACK = {
    POLICY: "This project is suspended on Codeskys. Contact your administrator.",
    CRED: "This project's Codeskys authorization is not valid (wrong, revoked, expired or replaced credential).",
    REPO: ("The repository is not this project's registered repository. If it was renamed, "
           "transferred or re-created, ask an administrator to re-register it."),
    AUTH: "Codeskys could not authorize this request.",
    REPLAY: "This deployment authorization was already used. Start a new deployment.",
    NOT_FOUND: "This project is not registered with Codeskys authorization.",
    INVALID: "Codeskys authorization rejected the request as invalid.",
    UNAVAILABLE: "The Codeskys authorization service is temporarily unavailable. Try again in a minute.",
}
_REASON_CODES = {
    "project_suspended": POLICY, "policy_blocked": POLICY,
    "invalid_credential": CRED, "status_revoked": CRED, "status_inactive": CRED, "status_rotated": CRED,
    "deployment_revoked": CRED, "deployment_superseded": CRED, "deployment_expired": CRED,
    "credential_expired": CRED, "repo_mismatch": REPO, "repo_not_registered": REPO,
    "repo_unverified": REPO, "deployment_mismatch": REPO, "replay_detected": REPLAY,
    "project_deleted": AUTH, "missing_credential": AUTH,
}
# authz's app-facing messages are deliberately terse upper-case banners; the
# dashboard shows a plain explanation instead.
_BANNER = re.compile(r"^[A-Z][A-Z ]{12,}\n")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class AuthzError(RuntimeError):
    """A refusal or failure from authz. ``status`` 0 = unreachable."""

    def __init__(self, status: int, code: str = "", reference: str = "", message: str = ""):
        self.status, self.code, self.reference = status, code, reference[:40]
        self.message = message[:300]
        super().__init__(self.describe())

    @property
    def retryable(self) -> bool:
        return self.status in (0, 429, 502, 503, 504)

    def user_message(self) -> str:
        if self.status == 401:
            # The *website's* key was refused: an operator problem, not the student's.
            msg = "Codeskys is misconfigured: the website could not authenticate to the authorization service."
        elif self.status == 0 or self.status >= 500:
            msg = (self.message if self.status in (502, 503) and self.message else _FALLBACK[UNAVAILABLE])
        elif self.message and not _BANNER.match(self.message):
            msg = self.message
        else:
            msg = _FALLBACK.get(self.code) or _FALLBACK.get({404: NOT_FOUND, 422: INVALID}.get(self.status, ""), "") \
                or "Codeskys authorization refused this request."
        return f"{msg} (reference {self.reference})" if self.reference else msg

    def describe(self) -> str:
        """Operator detail for logs. Contains no secrets (authz never echoes input)."""
        return " ".join(x for x in (f"authz {self.status or 'unreachable'}", self.code, self.reference) if x)


def _parse_error(status: int, body) -> AuthzError:
    code = reference = message = ""
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict):
            code, reference, message = (str(e.get(k) or "") for k in ("code", "reference", "message"))
        d = body.get("detail")
        if isinstance(d, dict):
            code = code or str(d.get("code") or "") or _REASON_CODES.get(str(d.get("reason") or ""), "")
            reference = reference or str(d.get("reference") or "")
        elif isinstance(d, str) and not message:
            message = d
        code = code or _REASON_CODES.get(str(body.get("reason") or ""), "") or str(body.get("code") or "")
        reference = reference or str(body.get("reference") or "")
    return AuthzError(status, code, reference, message)


def _seg(value) -> str:
    """A path segment we generated or stored ourselves; refuse anything else."""
    s = str(value)
    if not _ID.match(s):
        raise AuthzError(422, INVALID, "", "invalid identifier")
    return s


class AuthzClient:
    def __init__(self, base_url: str, key: str, timeout: float = 10.0, session=None):
        if not base_url or not key:
            raise ValueError("authz URL and website key are required")
        self.base = base_url.rstrip("/") + "/internal/website"
        self._key = key
        self.timeout = timeout
        self._http = session or requests

    def __repr__(self):
        return f"AuthzClient({self.base!r})"

    def _req(self, method: str, path: str, json=None) -> dict:
        headers = {"X-Website-Key": self._key, "X-Authz-Errors": "v2", "Accept": "application/json"}
        try:
            r = self._http.request(method, self.base + path, json=json, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise AuthzError(0, UNAVAILABLE, "", exc.__class__.__name__) from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400:
            raise _parse_error(r.status_code, body)
        if isinstance(body, dict) and body.get("ok") is False:   # legacy 200 denial
            raise _parse_error(200, body)
        return body if isinstance(body, dict) else {"data": body}

    # a1 / a2 / f1 / e2 -- projects
    def link_project(self, github_url: str, credential: str) -> dict:
        """Existing student projects: prove ownership with the current DEPLOY_CREDENTIAL.
        The credential is verified only (never rotated, stored or returned)."""
        return self._req("POST", "/projects/link", json={"github_url": github_url, "credential": credential})

    def create_project(self, *, student_name: str, project_name: str, repo_url: str,
                       email: str = "", github_owner: str = "", project_type: str = "") -> dict:
        body = {"student_name": student_name[:200], "project_name": project_name[:200], "repo_url": repo_url}
        for k, v in (("email", email), ("github_owner", github_owner), ("project_type", project_type)):
            if v:
                body[k] = v[:200]
        return self._req("POST", "/projects", json=body)

    def get_project(self, project_id: str) -> dict:
        return self._req("GET", f"/projects/{_seg(project_id)}")

    def suspend_project(self, project_id: str, suspended: bool, reason: str = "") -> dict:
        return self._req("POST", f"/projects/{_seg(project_id)}/suspend",
                         json={"suspended": bool(suspended), "reason": reason[:200]})

    # b / c -- repositories
    def register_repository(self, project_id: str, installation_id: int | None = None) -> dict:
        body = {"installation_id": int(installation_id)} if installation_id else {}
        return self._req("POST", f"/projects/{_seg(project_id)}/repository", json=body)

    def installation_repositories(self, installation_id: int) -> dict:
        return self._req("GET", f"/github/installations/{int(installation_id)}/repositories")

    # d / e1 / f2 -- deployments
    def create_deployment(self, project_id: str, github_repo_id: int, commit_sha: str = "") -> dict:
        return self._req("POST", "/deployments", json={
            "project_id": project_id, "github_repo_id": int(github_repo_id), "commit_sha": (commit_sha or "")[:64]})

    def get_deployment(self, deployment_id: str) -> dict:
        return self._req("GET", f"/deployments/{_seg(deployment_id)}")

    def revoke_deployment(self, deployment_id: str, reason: str = "revoked by platform") -> dict:
        return self._req("POST", f"/deployments/{_seg(deployment_id)}/revoke", json={"reason": reason[:200]})


def authz_settings(app_config) -> dict:
    return {"url": (app_config.get("AUTHZ_URL") or "").rstrip("/"),
            "key": app_config.get("AUTHZ_WEBSITE_KEY") or "",
            "required": bool(app_config.get("AUTHZ_REQUIRED"))}


def client_from_config(app_config) -> AuthzClient | None:
    """``None`` when authz is not configured (local development, simulator-only)."""
    s = authz_settings(app_config)
    if not (s["url"] and s["key"]):
        return None
    return AuthzClient(s["url"], s["key"])
