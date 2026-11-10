"""Billing: Razorpay Orders + Checkout, test mode by default.

Flow
  1. GET  /api/billing                      plan catalog, current plan, payment history
  2. POST /api/billing/orders {plan}        server creates a Razorpay order. The amount
                                            comes from the SERVER catalog, never the client.
  3. browser opens Razorpay Checkout with that order id; the student pays
  4. POST /api/billing/verify {...}         HMAC-SHA256(key_secret, "order|payment") must
                                            match, then the payment is fetched from Razorpay
                                            and must be captured, for this order, this amount.
  5. POST /api/hooks/razorpay               signed webhook (raw body HMAC with the webhook
                                            secret): authoritative, idempotent, replay-safe.

Entitlement: a paid payment extends users.plan_expires_at by the plan's period
(starting from the current expiry if the same plan is still active). It is granted
exactly once per payment (conditional UPDATE), whichever of verify/webhook arrives
first. A FULL refund takes the period back; a partial refund does not change access.
Plans set by an administrator (plan_expires_at NULL) are never touched by billing.

Safety
  * Only rzp_test_ keys work unless RAZORPAY_ALLOW_LIVE=1. Live activation is an
    owner decision (KYC, pricing, tax), never a default.
  * Not configured -> billing reports "not configured" and nothing can be bought.
  * Key secret and webhook secret never reach a response, the browser or a log line.
  * This is a payment receipt, not a GST tax invoice.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, abort, current_app, jsonify, render_template_string, request

from .auth import admin_required, current_user, login_required
from .config import _file_or_env
from .db import audit, get_db, utcnow
from .ratelimit import rate_limit

bp = Blueprint("billing", __name__)

# The public offer on the landing page: "STUDENT PLAN ₹500 / 6 months".
# It grants the existing "pro" plan limits. Change it with KOVA_BILLING_PLANS_JSON,
# not by editing code. Amounts are in paise.
DEFAULT_CATALOG = {
    "student-6m": {
        "name": "Student",
        "grants_plan": "pro",
        "amount": 50000,
        "currency": "INR",
        "period_days": 180,
        "description": "Codeskys Student plan, 6 months",
    },
}
SELLABLE_PLANS = {"pro", "team", "premium"}
CURRENCIES = {"INR"}
WEBHOOK_MAX_BYTES = 256 * 1024
WEBHOOK_MAX_AGE_S = 3 * 24 * 3600   # Razorpay retries for ~24 h; older bodies are replays
TERMINAL_OK = {"paid", "refunded", "partially_refunded"}


class BillingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ configuration

class BillingConfig:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        get = (lambda n: _file_or_env(n)) if env is os.environ else (lambda n: (env.get(n) or "").strip())
        self.key_id = get("RAZORPAY_KEY_ID")
        self.key_secret = get("RAZORPAY_KEY_SECRET")
        self.webhook_secret = get("RAZORPAY_WEBHOOK_SECRET")
        self.allow_live = (env.get("RAZORPAY_ALLOW_LIVE") or "").strip() == "1"
        # Owner switch: keys may be configured (webhooks, admin view) while nobody can
        # start a payment. Opened only after the owner confirms the final prices.
        self.checkout_open = (env.get("KOVA_BILLING_CHECKOUT") or "").strip() == "1"
        self.api_base = (env.get("RAZORPAY_API_BASE") or "https://api.razorpay.com/v1").rstrip("/")
        self.catalog, self._catalog_error = DEFAULT_CATALOG, ""
        raw = (env.get("KOVA_BILLING_PLANS_JSON") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict) or not parsed:
                    raise ValueError
                self.catalog = parsed
            except ValueError:
                self.catalog, self._catalog_error = {}, "KOVA_BILLING_PLANS_JSON is not a non-empty JSON object"

    @property
    def mode(self) -> str:
        if self.key_id.startswith("rzp_live_"):
            return "live"
        if self.key_id.startswith("rzp_test_"):
            return "test"
        return "unknown"

    def problems(self) -> list[str]:
        """Why billing is unusable, by setting NAME only (never values). [] = usable."""
        out = []
        if not self.key_id or not self.key_secret:
            return ["RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set"]
        if self.mode == "unknown":
            out.append("RAZORPAY_KEY_ID is not a Razorpay key id (rzp_test_ / rzp_live_)")
        if self.mode == "live" and not self.allow_live:
            out.append("live Razorpay keys are refused unless RAZORPAY_ALLOW_LIVE=1")
        if not self.webhook_secret:
            out.append("RAZORPAY_WEBHOOK_SECRET not set")
        if self._catalog_error:
            out.append(self._catalog_error)
        for code, p in self.catalog.items():
            if not isinstance(p, dict):
                out.append(f"plan {code!r}: not an object")
                continue
            if not isinstance(p.get("amount"), int) or p["amount"] < 100:
                out.append(f"plan {code!r}: amount must be an integer >= 100 (paise)")
            if p.get("currency") not in CURRENCIES:
                out.append(f"plan {code!r}: currency must be INR")
            if p.get("grants_plan") not in SELLABLE_PLANS:
                out.append(f"plan {code!r}: grants_plan must be one of {sorted(SELLABLE_PLANS)}")
            if not isinstance(p.get("period_days"), int) or not 1 <= p["period_days"] <= 3660:
                out.append(f"plan {code!r}: period_days must be 1..3660")
        return out

    @property
    def enabled(self) -> bool:
        return not self.problems()


def get_config() -> BillingConfig:
    cfg = current_app.config.get("_BILLING")
    if cfg is None:
        cfg = BillingConfig()
        current_app.config["_BILLING"] = cfg
    return cfg


def _http():
    """requests-compatible session; tests inject a fake via app.config["BILLING_HTTP"]."""
    return current_app.config.get("BILLING_HTTP") or requests


# ------------------------------------------------------------------ helpers

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def checkout_signature_valid(key_secret, order_id, payment_id, signature) -> bool:
    if not (key_secret and order_id and payment_id and signature):
        return False
    return hmac.compare_digest(_hmac_hex(key_secret, f"{order_id}|{payment_id}".encode()), str(signature))


def webhook_signature_valid(secret, raw: bytes, signature) -> bool:
    if not (secret and signature):
        return False
    return hmac.compare_digest(_hmac_hex(secret, raw), str(signature))


def _log(event: str, **fields):
    # Structured, secret-free. Only ids and states.
    current_app.logger.info("billing %s %s", event, json.dumps(fields, sort_keys=True))


def _razorpay(cfg: BillingConfig, method: str, path: str, **kw):
    try:
        r = _http().request(method, cfg.api_base + path, auth=(cfg.key_id, cfg.key_secret), timeout=10, **kw)
    except requests.RequestException as exc:
        _log("provider_unreachable", path=path.split("/")[1], error=type(exc).__name__)
        raise BillingError("The payment provider could not be reached. Try again shortly.", 502)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 300:
        _log("provider_rejected", path=path.split("/")[1], status=r.status_code)
        raise BillingError("The payment provider rejected the request.", 502)
    return data


def _payment_out(row, include_user=False) -> dict:
    out = {"ref": row["ref"], "plan": row["plan"], "grants_plan": row["grants_plan"],
           "period_days": row["period_days"], "amount": row["amount"], "currency": row["currency"],
           "mode": row["mode"], "status": row["status"], "refunded_amount": row["refunded_amount"],
           "entitlement_until": row["entitlement_until"], "created_at": row["created_at"],
           "paid_at": row["paid_at"], "failure_reason": row["failure_reason"],
           "receipt_available": row["status"] in TERMINAL_OK}
    if include_user:
        out.update(user_id=row["user_id"], razorpay_order_id=row["razorpay_order_id"],
                   razorpay_payment_id=row["razorpay_payment_id"])
    return out


def _user_plan_state(user) -> dict:
    keys = user.keys()
    exp = user["plan_expires_at"] if "plan_expires_at" in keys else None
    return {"plan": user["plan"], "plan_expires_at": exp,
            "managed_by_admin": user["plan"] != "hobby" and not exp}


# ------------------------------------------------------------------ state transitions
# All transitions are idempotent and safe to run from verify AND webhook concurrently.

def _grant(db, pay) -> None:
    """Extend the user's plan once for this payment."""
    cur = db.execute("UPDATE payments SET entitlement_granted = 1, updated_at = ? "
                     "WHERE id = ? AND entitlement_granted = 0", (utcnow(), pay["id"]))
    if cur.rowcount != 1:
        return  # already granted (or revoked) by an earlier event
    user = db.execute("SELECT * FROM users WHERE id = ?", (pay["user_id"],)).fetchone()
    now = _now()
    current_exp = _parse(user["plan_expires_at"])
    start = current_exp if (user["plan"] == pay["grants_plan"] and current_exp and current_exp > now) else now
    until = _iso(start + timedelta(days=pay["period_days"]))
    if user["plan"] != "hobby" and not user["plan_expires_at"]:
        # An administrator set this plan; never override it. The payment is still recorded.
        db.execute("UPDATE payments SET entitlement_until = NULL, failure_reason = ? WHERE id = ?",
                   ("plan is administrator-managed; access unchanged", pay["id"]))
        _log("grant_skipped_admin_plan", ref=pay["ref"])
        return
    db.execute("UPDATE users SET plan = ?, plan_expires_at = ? WHERE id = ?", (pay["grants_plan"], until, user["id"]))
    db.execute("UPDATE payments SET entitlement_until = ? WHERE id = ?", (until, pay["id"]))
    audit("billing.plan_granted", actor_id=user["id"], target=pay["ref"], detail=f"{pay['grants_plan']} until {until}")
    _log("plan_granted", ref=pay["ref"], plan=pay["grants_plan"], until=until)


def _revoke(db, pay) -> None:
    """Full refund: take back exactly the period this payment added (once)."""
    cur = db.execute("UPDATE payments SET entitlement_granted = 2, updated_at = ? "
                     "WHERE id = ? AND entitlement_granted = 1", (utcnow(), pay["id"]))
    if cur.rowcount != 1 or not pay["entitlement_until"]:
        return
    user = db.execute("SELECT * FROM users WHERE id = ?", (pay["user_id"],)).fetchone()
    exp = _parse(user["plan_expires_at"])
    if user["plan"] != pay["grants_plan"] or exp is None:
        return
    new_exp = exp - timedelta(days=pay["period_days"])
    if new_exp <= _now():
        db.execute("UPDATE users SET plan = 'hobby', plan_expires_at = NULL WHERE id = ?", (user["id"],))
    else:
        db.execute("UPDATE users SET plan_expires_at = ? WHERE id = ?", (_iso(new_exp), user["id"]))
    audit("billing.plan_revoked", actor_id=user["id"], target=pay["ref"], detail="full refund")
    _log("plan_revoked", ref=pay["ref"])


def _mark_paid(db, pay, payment_id: str, source: str) -> None:
    if pay["status"] not in TERMINAL_OK:
        db.execute("UPDATE payments SET status = 'paid', razorpay_payment_id = ?, paid_at = ?, "
                   "failure_reason = '', updated_at = ? WHERE id = ? AND status NOT IN ('paid','refunded','partially_refunded')",
                   (payment_id, utcnow(), utcnow(), pay["id"]))
        _log("payment_paid", ref=pay["ref"], source=source)
    pay = db.execute("SELECT * FROM payments WHERE id = ?", (pay["id"],)).fetchone()
    if pay["status"] == "paid":
        _grant(db, pay)


def _mark_failed(db, pay, reason: str) -> None:
    # A later attempt on the same order may still succeed: failed -> paid is allowed,
    # paid -> failed is not.
    db.execute("UPDATE payments SET status = 'failed', failure_reason = ?, updated_at = ? "
               "WHERE id = ? AND status IN ('created','pending')", (reason[:200], utcnow(), pay["id"]))


def _apply_refund(db, pay, refund_id: str, amount: int, status: str, requested_by=None) -> None:
    db.execute(
        "INSERT INTO payment_refunds (payment_id, razorpay_refund_id, amount, status, requested_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(razorpay_refund_id) DO UPDATE SET status = excluded.status, "
        "updated_at = excluded.updated_at", (pay["id"], refund_id, amount, status, requested_by, utcnow(), utcnow()))
    refunded = db.execute("SELECT COALESCE(SUM(amount),0) AS s FROM payment_refunds WHERE payment_id = ? AND status = 'processed'",
                          (pay["id"],)).fetchone()["s"]
    if refunded <= 0:
        return
    new_status = "refunded" if refunded >= pay["amount"] else "partially_refunded"
    db.execute("UPDATE payments SET refunded_amount = ?, status = ?, updated_at = ? WHERE id = ?",
               (min(refunded, pay["amount"]), new_status, utcnow(), pay["id"]))
    if new_status == "refunded":
        _revoke(db, db.execute("SELECT * FROM payments WHERE id = ?", (pay["id"],)).fetchone())


def _settle_from_provider(db, cfg, pay, payment: dict, source: str) -> str:
    """Apply a Razorpay payment entity to our row after checking it belongs to it."""
    if payment.get("order_id") != pay["razorpay_order_id"]:
        raise BillingError("Payment does not belong to this order.", 400)
    if payment.get("amount") != pay["amount"] or payment.get("currency") != pay["currency"]:
        _log("amount_mismatch", ref=pay["ref"], source=source)
        db.execute("UPDATE payments SET failure_reason = ?, updated_at = ? WHERE id = ?",
                   ("provider amount/currency mismatch", utcnow(), pay["id"]))
        return "mismatch"
    status = payment.get("status")
    if status == "captured" or (status == "refunded" and pay["status"] in TERMINAL_OK):
        _mark_paid(db, pay, payment.get("id", ""), source)
        return "paid"
    if status == "authorized":
        db.execute("UPDATE payments SET status = 'pending', razorpay_payment_id = ?, updated_at = ? "
                   "WHERE id = ? AND status IN ('created','failed')", (payment.get("id", ""), utcnow(), pay["id"]))
        return "pending"
    if status == "failed":
        _mark_failed(db, pay, str(payment.get("error_description") or "payment failed"))
        return "failed"
    return "pending"


# ------------------------------------------------------------------ student endpoints

def _error(exc: BillingError):
    return jsonify(error=str(exc)), exc.status


@bp.get("/api/billing")
@login_required
def billing_home():
    cfg = get_config()
    user = current_user()
    rows = get_db().execute("SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT 50", (user["id"],)).fetchall()
    return jsonify(
        enabled=cfg.enabled,
        checkout_open=cfg.enabled and cfg.checkout_open,
        mode=cfg.mode if cfg.enabled else "disabled",
        key_id=cfg.key_id if cfg.enabled else "",   # public by design: Checkout needs it
        catalog=[{"code": c, "name": p.get("name", c), "grants_plan": p.get("grants_plan"),
                  "amount": p.get("amount"), "currency": p.get("currency"),
                  "period_days": p.get("period_days"), "description": p.get("description", "")}
                 for c, p in cfg.catalog.items()] if cfg.enabled else [],
        account=_user_plan_state(user),
        payments=[_payment_out(r) for r in rows],
    )


@bp.post("/api/billing/orders")
@login_required
def create_order():
    cfg = get_config()
    if not cfg.enabled:
        return jsonify(error="Payments are not configured yet."), 503
    if not cfg.checkout_open:
        return jsonify(error="Checkout is not open yet. Prices are awaiting final confirmation."), 503
    user = current_user()
    db = get_db()
    if rate_limit(db, f"billing:order:{user['id']}", 10, 600):
        return jsonify(error="Too many payment attempts. Wait a few minutes and try again."), 429
    code = str((request.get_json(silent=True) or {}).get("plan", ""))[:60]
    plan = cfg.catalog.get(code)
    if plan is None:
        return jsonify(error="Unknown plan."), 422
    state = _user_plan_state(user)
    if state["managed_by_admin"]:
        return jsonify(error="Your plan is managed by an administrator. Contact them to change it."), 409
    if user["plan"] not in ("hobby", plan["grants_plan"]):
        return jsonify(error=f"You already have an active {user['plan'].title()} plan."), 409

    ref = "pay_" + secrets.token_hex(8)
    try:
        data = _razorpay(cfg, "POST", "/orders", json={
            "amount": plan["amount"], "currency": plan["currency"], "receipt": ref,
            "notes": {"plan": code, "ref": ref}})
    except BillingError as exc:
        return _error(exc)
    if (data.get("amount") != plan["amount"] or data.get("currency") != plan["currency"]
            or not str(data.get("id", "")).startswith("order_")):
        _log("order_unexpected", ref=ref)
        return jsonify(error="The payment provider returned an unexpected order."), 502
    now = utcnow()
    db.execute("INSERT INTO payments (ref, user_id, plan, grants_plan, period_days, amount, currency, mode, status, "
               "razorpay_order_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (ref, user["id"], code, plan["grants_plan"], plan["period_days"], plan["amount"], plan["currency"],
                cfg.mode, "created", data["id"], now, now))
    db.commit()
    audit("billing.order_created", actor_id=user["id"], target=ref, ip=request.remote_addr)
    _log("order_created", ref=ref, plan=code, mode=cfg.mode)
    return jsonify(ref=ref, order_id=data["id"], amount=plan["amount"], currency=plan["currency"],
                   key_id=cfg.key_id, name=current_app.config["PLATFORM_NAME"],
                   description=plan.get("description", ""), mode=cfg.mode,
                   prefill={"name": user["name"], "email": user["email"]}), 201


@bp.post("/api/billing/verify")
@login_required
def verify():
    cfg = get_config()
    if not cfg.enabled:
        return jsonify(error="Payments are not configured yet."), 503
    body = request.get_json(silent=True) or {}
    order_id = str(body.get("razorpay_order_id", ""))[:64]
    payment_id = str(body.get("razorpay_payment_id", ""))[:64]
    signature = str(body.get("razorpay_signature", ""))[:128]
    user = current_user()
    db = get_db()
    pay = db.execute("SELECT * FROM payments WHERE razorpay_order_id = ? AND user_id = ?", (order_id, user["id"])).fetchone()
    if pay is None or not checkout_signature_valid(cfg.key_secret, order_id, payment_id, signature):
        _log("verify_failed", known_order=pay is not None)
        return jsonify(error="We could not verify this payment. If money was taken, it will be "
                             "confirmed automatically or refunded by the provider."), 400
    try:
        entity = _razorpay(cfg, "GET", f"/payments/{payment_id}")
        outcome = _settle_from_provider(db, cfg, pay, entity, "checkout")
    except BillingError as exc:
        if exc.status == 502:
            # Signature is genuine but we cannot confirm capture right now: the signed
            # webhook will settle it. Never grant access on the signature alone.
            db.execute("UPDATE payments SET status = 'pending', razorpay_payment_id = ?, updated_at = ? "
                       "WHERE id = ? AND status IN ('created','failed')", (payment_id, utcnow(), pay["id"]))
            outcome = "pending"
        else:
            db.rollback()
            return _error(exc)
    db.commit()
    pay = db.execute("SELECT * FROM payments WHERE id = ?", (pay["id"],)).fetchone()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return jsonify(outcome=outcome, payment=_payment_out(pay), account=_user_plan_state(user))


@bp.get("/api/billing/payments/<ref>")
@login_required
def payment_status(ref):
    user = current_user()
    q = "SELECT * FROM payments WHERE ref = ?" + ("" if user["role"] == "admin" else " AND user_id = ?")
    args = (ref,) if user["role"] == "admin" else (ref, user["id"])
    row = get_db().execute(q, args).fetchone()
    if row is None:
        return jsonify(error="Not found."), 404
    return jsonify(payment=_payment_out(row))


RECEIPT_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Receipt {{ p.ref }}</title>
<style>body{font:15px/1.5 system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#111}
table{width:100%;border-collapse:collapse}td{padding:6px 0;border-bottom:1px solid #ddd}td:last-child{text-align:right}
.test{background:#fff3cd;padding:8px 12px;border-radius:6px}small{color:#555}</style></head><body>
<h1>{{ platform }} payment receipt</h1>
{% if p.mode != 'live' %}<p class="test"><b>TEST MODE.</b> No real money was charged.</p>{% endif %}
<table>
<tr><td>Receipt number</td><td>{{ p.ref }}</td></tr>
<tr><td>Billed to</td><td>{{ u.name }} &lt;{{ u.email }}&gt;</td></tr>
<tr><td>Plan</td><td>{{ p.plan }} ({{ p.period_days }} days of {{ p.grants_plan|title }})</td></tr>
<tr><td>Amount</td><td>{{ p.currency }} {{ '%.2f'|format(p.amount / 100) }}</td></tr>
{% if p.refunded_amount %}<tr><td>Refunded</td><td>{{ p.currency }} {{ '%.2f'|format(p.refunded_amount / 100) }}</td></tr>{% endif %}
<tr><td>Status</td><td>{{ p.status }}</td></tr>
<tr><td>Paid at (UTC)</td><td>{{ p.paid_at }}</td></tr>
<tr><td>Provider payment id</td><td>{{ p.razorpay_payment_id }}</td></tr>
</table>
<p><small>This is a payment receipt, not a GST tax invoice.</small></p>
</body></html>"""


@bp.get("/billing/receipts/<ref>")
@login_required
def receipt(ref):
    user = current_user()
    db = get_db()
    q = "SELECT * FROM payments WHERE ref = ?" + ("" if user["role"] == "admin" else " AND user_id = ?")
    row = db.execute(q, (ref,) if user["role"] == "admin" else (ref, user["id"])).fetchone()
    if row is None or row["status"] not in TERMINAL_OK:
        abort(404)
    owner = db.execute("SELECT name, email FROM users WHERE id = ?", (row["user_id"],)).fetchone()
    return render_template_string(RECEIPT_HTML, p=row, u=owner, platform=current_app.config["PLATFORM_NAME"])


# ------------------------------------------------------------------ webhook

@bp.post("/api/hooks/razorpay")
def webhook():
    cfg = get_config()
    raw = request.get_data(cache=False)
    if len(raw) > WEBHOOK_MAX_BYTES:
        return jsonify(error="Payload too large."), 413
    if not webhook_signature_valid(cfg.webhook_secret, raw, request.headers.get("X-Razorpay-Signature", "")):
        _log("webhook_rejected")
        return jsonify(error="Invalid signature."), 400
    try:
        evt = json.loads(raw)
        if not isinstance(evt, dict):
            raise ValueError
    except ValueError:
        return jsonify(error="Invalid payload."), 400

    created = evt.get("created_at")
    if isinstance(created, int) and _now().timestamp() - created > WEBHOOK_MAX_AGE_S:
        _log("webhook_stale")
        return jsonify(ok=True, ignored="stale")

    db = get_db()
    # Dedupe by the (unsigned) event id header AND by the signed body itself, so a
    # replayed body with a fresh header is still recognised.
    keys = ["body:" + hashlib.sha256(raw).hexdigest()]
    header_id = (request.headers.get("X-Razorpay-Event-Id") or "")[:64]
    if header_id:
        keys.append("id:" + header_id)
    name = str(evt.get("event", ""))[:60]
    try:
        if any(db.execute("SELECT 1 FROM payment_events WHERE event_id = ?", (k,)).fetchone() for k in keys):
            return jsonify(ok=True, duplicate=True)
        for k in keys:
            db.execute("INSERT INTO payment_events (event_id, event, received_at) VALUES (?,?,?)", (k, name, utcnow()))
        result = _handle_event(db, cfg, name, evt.get("payload") or {})
        db.commit()
    except Exception:
        db.rollback()
        _log("webhook_error", event=name)
        return jsonify(error="Temporary error."), 500   # Razorpay retries
    return jsonify(ok=True, **result)


def _handle_event(db, cfg, name: str, payload: dict) -> dict:
    payment = ((payload.get("payment") or {}).get("entity")) or {}
    refund = ((payload.get("refund") or {}).get("entity")) or {}
    order = ((payload.get("order") or {}).get("entity")) or {}
    order_id = payment.get("order_id") or order.get("id")
    pay = None
    if order_id:
        pay = db.execute("SELECT * FROM payments WHERE razorpay_order_id = ?", (order_id,)).fetchone()
    elif refund.get("payment_id"):
        pay = db.execute("SELECT * FROM payments WHERE razorpay_payment_id = ?", (refund["payment_id"],)).fetchone()
    if pay is None:
        _log("webhook_unknown_order", event=name)
        return {"ignored": "unknown order"}

    if name in ("payment.captured", "order.paid"):
        if not payment:
            return {"ignored": "no payment entity"}
        try:
            return {"outcome": _settle_from_provider(db, cfg, pay, payment, "webhook")}
        except BillingError:
            return {"ignored": "payment does not match order"}
    if name == "payment.authorized" and payment:
        try:
            return {"outcome": _settle_from_provider(db, cfg, pay, payment, "webhook")}
        except BillingError:
            return {"ignored": "payment does not match order"}
    if name == "payment.failed":
        _mark_failed(db, pay, str(payment.get("error_description") or "payment failed"))
        return {"outcome": "failed"}
    if name in ("refund.processed", "refund.created", "refund.failed") and refund.get("id"):
        if refund.get("payment_id") and pay["razorpay_payment_id"] and refund["payment_id"] != pay["razorpay_payment_id"]:
            return {"ignored": "refund does not match payment"}
        status = {"refund.processed": "processed", "refund.failed": "failed"}.get(name, refund.get("status") or "pending")
        amount = refund.get("amount")
        if not isinstance(amount, int) or amount <= 0 or amount > pay["amount"]:
            return {"ignored": "invalid refund amount"}
        _apply_refund(db, pay, refund["id"], amount, status)
        return {"outcome": "refund_" + status}
    return {"ignored": "event not handled"}


# ------------------------------------------------------------------ admin

@bp.get("/api/admin/billing")
@admin_required
def admin_payments():
    cfg = get_config()
    rows = get_db().execute("SELECT * FROM payments ORDER BY id DESC LIMIT 200").fetchall()
    return jsonify(enabled=cfg.enabled, mode=cfg.mode if cfg.enabled else "disabled",
                   checkout_open=cfg.enabled and cfg.checkout_open,
                   problems=cfg.problems() + ([] if cfg.checkout_open else ["checkout closed (KOVA_BILLING_CHECKOUT is not 1)"]), payments=[_payment_out(r, include_user=True) for r in rows])


@bp.post("/api/admin/billing/payments/<ref>/refund")
@admin_required
def admin_refund(ref):
    cfg = get_config()
    if not cfg.enabled:
        return jsonify(error="Payments are not configured yet."), 503
    db = get_db()
    pay = db.execute("SELECT * FROM payments WHERE ref = ?", (ref,)).fetchone()
    if pay is None:
        return jsonify(error="Not found."), 404
    if pay["status"] not in ("paid", "partially_refunded") or not pay["razorpay_payment_id"]:
        return jsonify(error="Only paid payments can be refunded."), 409
    remaining = pay["amount"] - pay["refunded_amount"]
    amount = (request.get_json(silent=True) or {}).get("amount", remaining)
    if not isinstance(amount, int) or not 100 <= amount <= remaining:
        return jsonify(error=f"Refund amount must be between 100 and {remaining} paise."), 422
    admin = current_user()
    try:
        data = _razorpay(cfg, "POST", f"/payments/{pay['razorpay_payment_id']}/refund",
                         json={"amount": amount, "notes": {"ref": ref, "by": str(admin["id"])}})
    except BillingError as exc:
        return _error(exc)
    if not str(data.get("id", "")).startswith("rfnd_") or data.get("amount") != amount:
        return jsonify(error="The payment provider returned an unexpected refund."), 502
    status = data.get("status") if data.get("status") in ("pending", "processed", "failed") else "pending"
    _apply_refund(db, pay, data["id"], amount, status, requested_by=admin["id"])
    db.commit()
    audit("billing.refund_requested", actor_id=admin["id"], target=ref, detail=f"{amount} {status}")
    pay = db.execute("SELECT * FROM payments WHERE id = ?", (pay["id"],)).fetchone()
    return jsonify(refund={"id": data["id"], "amount": amount, "status": status}, payment=_payment_out(pay, True))
