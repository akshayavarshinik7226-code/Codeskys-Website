"""Plan rules in one place: how long a project stays live, and who may deploy.

The Free (Hobby) rules, exactly as the owner defined them:

* A free user has ONE project, live for ONE window of 24 hours.
* The window starts when the project first goes live. Re-deploying or re-uploading inside it
  keeps the SAME expiry; a redeploy never starts a new clock.
* When the window ends the link shows a 404 page and the user is asked to upgrade. From then
  on the project cannot be deployed again on the free plan, and deleting it does not give a
  second free window (``users.free_started_at``).

Paid hosting (billing.py: 6 months for ₹510, or lifetime for ₹1,599) sets ``users.hosting_until``.
While it runs a person's deployments are not limited to 24 hours: they stay live until the paid
period ends plus a 24-hour grace (engine.hosting_expiry). A paid person may have many projects.
An older administrator-set plan (pro/team with an optional end date) is honoured the same way.

Administrator accounts are exempt from all of this (they are used to test the platform).
"""
from datetime import datetime, timedelta, timezone

from .db import utcnow

FREE = "hobby"
PAID_PROJECT_LIMIT = 100        # how many projects a paying person may have
LIFETIME_YEAR = 2100            # a hosting end date this far away means "lifetime"

FREE_ENDED = ("Your free 24 hours have ended, so this project is no longer live. "
              "Upgrade to bring it back online.")
FREE_USED = ("You have already used your free 24-hour project. "
             "Upgrade your plan to publish another project.")
PAID_ENDED = ("Your paid hosting has ended, so this project is no longer live. "
              "Renew to bring it back online.")


class PlanBlocked(Exception):
    """The plan does not allow this action. Always phrased for the person using the site."""

    def __init__(self, message, code="upgrade_required"):
        super().__init__(message)
        self.message = message
        self.code = code


def _owner(db, project):
    return db.execute("SELECT * FROM users WHERE id = ?", (project["owner_id"],)).fetchone()


def _col(row, name):
    return row[name] if row is not None and name in row.keys() else None


def is_admin(user) -> bool:
    return user is not None and user["role"] == "admin"


def hosting_active(db, user) -> str | None:
    """When paid hosting stops counting (end + grace), or None if the person has none running."""
    if user is None or not _col(user, "hosting_until"):
        return None
    from . import engine
    return engine.hosting_expiry(db, user["id"])


def plan_active(user) -> bool:
    """An older paid plan (pro/team) set by an administrator or a plan payment, still in date."""
    if user is None or user["plan"] == FREE:
        return False
    end = _col(user, "plan_expires_at")
    return not end or end > utcnow()


def is_paid(db, user) -> bool:
    return bool(hosting_active(db, user)) or plan_active(user)


def is_lifetime(user) -> bool:
    end = _col(user, "hosting_until")
    return bool(end) and int(end[:4]) >= LIFETIME_YEAR


def is_free(db, user) -> bool:
    return user is not None and not is_admin(user) and not is_paid(db, user)


def project_limit(db, user) -> int:
    """How many projects this person may have."""
    from .projects import PLAN_LIMITS
    if is_admin(user):
        return 10 ** 6
    if is_paid(db, user):
        planned = PLAN_LIMITS.get(user["plan"], {}).get("projects", 0) if plan_active(user) else 0
        return max(PAID_PROJECT_LIMIT, planned)
    return PLAN_LIMITS[FREE]["projects"]


def limits_for(db, user) -> dict:
    """The plan limits shown on the dashboard (paid hosting gets the paid limits)."""
    from .projects import PLAN_LIMITS
    if is_admin(user):
        return {**PLAN_LIMITS["team"], "projects": project_limit(db, user)}
    if is_paid(db, user):
        base = PLAN_LIMITS.get(user["plan"]) if plan_active(user) else None
        return {**(base or PLAN_LIMITS["pro"]), "projects": project_limit(db, user)}
    return PLAN_LIMITS[FREE]


def free_clock(db, project):
    """The single expiry of a free project, or None if it has not gone live yet."""
    row = db.execute("SELECT free_expires_at FROM projects WHERE id = ?", (project["id"],)).fetchone()
    return row["free_expires_at"] if row else None


def _was_live_before(db, project) -> bool:
    return db.execute("SELECT 1 FROM deployments WHERE project_id = ? AND status IN ('ready','expired') LIMIT 1",
                      (project["id"],)).fetchone() is not None


def state(db, project, owner=None) -> dict:
    """What the dashboard needs to show for a project's plan and expiry."""
    owner = owner or _owner(db, project)
    now = utcnow()
    if owner is None:
        return {"plan": FREE, "is_free": True, "expires_at": None, "expired": False, "blocked": False,
                "reason": None, "lifetime": False}
    if is_admin(owner):
        # Administrators are staff and use this account to test the platform: none of the plan
        # limits or the expiry apply, so nothing they deploy ever expires or gets blocked.
        return {"plan": owner["plan"], "is_free": False, "expires_at": None, "expired": False, "blocked": False,
                "reason": None, "lifetime": False, "admin": True}
    if is_paid(db, owner):
        life = is_lifetime(owner)
        exp = None if life else (_col(owner, "hosting_until") or _col(owner, "plan_expires_at"))
        return {"plan": owner["plan"], "is_free": False, "expires_at": exp, "expired": False, "blocked": False,
                "reason": None, "lifetime": life}
    clock = free_clock(db, project)
    expired = bool(clock and clock <= now)
    reason = None
    if expired or (not clock and _was_live_before(db, project)):
        reason = PAID_ENDED if _col(owner, "hosting_until") else FREE_ENDED
    return {"plan": FREE, "is_free": True, "expires_at": clock, "expired": expired or reason is not None,
            "blocked": reason is not None, "reason": reason, "lifetime": False}


def ensure_can_deploy(db, project):
    """Raise PlanBlocked unless this project may be deployed (or re-uploaded) right now."""
    st = state(db, project)
    if st["blocked"]:
        raise PlanBlocked(st["reason"])


def ensure_can_create(db, user):
    """A free user gets one free window for life; deleting an expired project does not reset it."""
    if is_admin(user) or is_paid(db, user):
        return
    row = db.execute("SELECT free_started_at FROM users WHERE id = ?", (user["id"],)).fetchone()
    if row and row["free_started_at"]:
        raise PlanBlocked(FREE_USED)


def expiry_for_new_deployment(db, project, ttl_hours=None):
    """The expiry a freshly live deployment gets (ISO text), or None for "no end date".

    Free project: starts the one clock the first time and returns that same value ever after
    (the project row is read again here because the copy handed to the build worker can be
    stale). Paid hosting: the end of the paid period plus grace. Administrator: no end.
    """
    owner = _owner(db, project)
    if is_admin(owner):
        return None
    if owner is not None and is_paid(db, owner):
        ends = []
        host = hosting_active(db, owner)
        if host:
            ends.append(host)
        if plan_active(owner):
            if not _col(owner, "plan_expires_at"):
                return None                      # an administrator-set plan with no end date
            ends.append(owner["plan_expires_at"])
        return max(ends) if ends else None
    existing = free_clock(db, project)
    if existing:
        return existing
    hours = ttl_hours or project["ttl_hours"] or 24
    fresh = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
    # Only the first writer wins, so two builds finishing together still share one clock.
    db.execute("UPDATE projects SET free_expires_at = ? WHERE id = ? AND free_expires_at IS NULL",
               (fresh, project["id"]))
    if owner is not None:
        db.execute("UPDATE users SET free_started_at = COALESCE(free_started_at, ?) WHERE id = ?",
                   (utcnow(), owner["id"]))
    db.commit()
    return free_clock(db, project)


def apply_plan_to_live_projects(db, user_id) -> int:
    """After a plan change: everything this person has live now lasts as long as the paid plan."""
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or is_admin(user) or not plan_active(user):
        return 0
    cur = db.execute(
        "UPDATE deployments SET expires_at = ? WHERE status = 'ready' AND expired_at IS NULL "
        "AND project_id IN (SELECT id FROM projects WHERE owner_id = ?)",
        (_col(user, "plan_expires_at"), user_id))
    db.commit()
    return cur.rowcount
