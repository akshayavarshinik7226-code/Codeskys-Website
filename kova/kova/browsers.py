"""One account per browser, like other platforms.

Why: the free plan gives one project for 24 hours. If a person could simply open a second account in
the same browser, that rule would mean nothing. So every browser gets a random token (a long-lived
cookie); its hash is linked to the first ordinary account used there.

* Creating another account in a linked browser is refused ("Log In with your existing account").
* Logging in as a *different* ordinary account in a linked browser is refused.
* Administrators are exempt. A browser that has ever signed in as an administrator is flagged and is
  unlimited, so the administrator can create and test as many user accounts as needed in it.

Honest limit: this is cookie based, so it stops casual multi-account use, not a determined person who
clears cookies or uses another browser. (Real abuse protection needs payment or phone verification.)
Switch off with KOVA_ONE_ACCOUNT_PER_BROWSER=0.
"""
import hashlib
import re
import secrets

from flask import current_app, g, request

from .db import utcnow

COOKIE = "kova_dev"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{30,80}$")
TEN_YEARS = 10 * 365 * 24 * 3600


def _enabled() -> bool:
    return bool(current_app.config.get("ONE_ACCOUNT_PER_BROWSER"))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token(create: bool):
    tok = request.cookies.get(COOKIE, "")
    if _TOKEN_RE.match(tok or ""):
        return tok
    if not create:
        return None
    tok = g.get("new_device_token") or secrets.token_urlsafe(32)
    g.new_device_token = tok          # sent back to the browser by attach_cookie()
    return tok


def _link(db, create=False):
    tok = _token(create)
    if tok is None:
        return None, None
    row = db.execute("SELECT * FROM browser_links WHERE device_hash = ?", (_hash(tok),)).fetchone()
    return tok, row


def _mask(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return (local[:1] + "\u2022\u2022\u2022" + "@" + domain) if local else "another account"


def _save(db, tok, user_id, admin):
    now = utcnow()
    db.execute("INSERT INTO browser_links (device_hash, user_id, is_admin_device, created_at, last_seen_at) "
               "VALUES (?,?,?,?,?) ON CONFLICT(device_hash) DO UPDATE SET "
               "user_id = COALESCE(excluded.user_id, browser_links.user_id), "
               "is_admin_device = MAX(browser_links.is_admin_device, excluded.is_admin_device), "
               "last_seen_at = excluded.last_seen_at", (_hash(tok), user_id, 1 if admin else 0, now, now))
    db.commit()


def check_signup(db):
    """None if a new account may be created in this browser, otherwise the message to show."""
    if not _enabled():
        return None
    tok, link = _link(db, create=True)
    if link is not None and (link["is_admin_device"] or not link["user_id"]):
        return None
    if link is not None:
        return "This browser already has a Codeskys account. Please Log In with it instead of creating another one."
    return None


def check_login(db, user):
    """None if ``user`` may sign in here (and links the browser), otherwise the message to show."""
    if not _enabled():
        return None
    tok, link = _link(db, create=True)
    if user["role"] == "admin":
        _save(db, tok, None, admin=True)      # an administrator's browser is unlimited
        return None
    if link is not None and link["is_admin_device"]:
        return None
    if link is not None and link["user_id"] and link["user_id"] != user["id"]:
        owner = db.execute("SELECT email FROM users WHERE id = ?", (link["user_id"],)).fetchone()
        return ("This browser is already linked to another Codeskys account (" + _mask(owner["email"] if owner else "") +
                "). Log In with that account, or use a different browser.")
    _save(db, tok, user["id"], admin=False)
    return None


def bind(db, user):
    """A new account was just created in this browser: link it (unless this is an administrator's browser)."""
    if not _enabled():
        return
    tok, link = _link(db, create=True)
    if link is not None and link["is_admin_device"]:
        return
    _save(db, tok, user["id"], admin=(user["role"] == "admin"))


def attach_cookie(resp):
    tok = g.get("new_device_token")
    if tok:
        resp.set_cookie(COOKIE, tok, max_age=TEN_YEARS, httponly=True, samesite="Lax",
                        secure=bool(current_app.config.get("SESSION_COOKIE_SECURE")))
    return resp
