"""Manual UPI payments: pay -> type the transaction ID -> an administrator approves.

The flow, as the owner designed it:

1. A person opens Usage & Billing, picks Paid Hosting (6 months, Rs 510) or Lifetime (Rs 1,599).
   Codeskys creates an order with a reference such as ``CSK-7H2K9QXM`` and shows a receipt page
   with every detail and a "Pay with UPI" link (``upi://pay?...``) to the Codeskys UPI ID.
2. They pay in their own UPI app, then type the 12-digit UPI transaction ID (UTR) on that page.
3. The page says "verifying your payment". Every payment is appended to a sheet
   (``instance/upi_payments.csv``, opens in Excel) with the person's name, email, plan, amount and time;
   the only thing the person types is the transaction ID.
4. The administrator checks the bank app on the short Payments page and presses Approve. Approval
   sets ``users.hosting_until`` exactly like a Razorpay payment does, and live projects are extended.

UPI cannot confirm a payment by itself, so a person could type a made-up number. That is why nothing
is granted until an administrator approves, and why one transaction ID can only be used once.
"""
import csv
import io
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from flask import Blueprint, Response, abort, current_app, jsonify, render_template_string, request

from .auth import admin_required, current_user, login_required
from .db import audit, get_db, utcnow
from .ratelimit import rate_limit

bp = Blueprint("upi", __name__)

UTR_RE = re.compile(r"^\d{12}$")
MAX_ATTEMPTS = 3
IST = timezone(timedelta(hours=5, minutes=30))          # a fixed offset: no time-zone database needed
LIFETIME_YEAR = 2100
STATUS_LABEL = {"awaiting_payment": "Awaiting payment", "submitted": "Verifying", "approved": "Approved",
                "rejected": "Not verified", "cancelled": "Cancelled"}


def _enabled() -> bool:
    return bool(current_app.config.get("UPI_ENABLED") and current_app.config.get("UPI_ID"))


def offers() -> dict:
    """The paid-hosting offers (same catalog as card payments): code -> offer."""
    from .billing import HOSTING, get_config
    return {c: p for c, p in get_config().catalog.items() if p.get("grants_plan") == HOSTING}


def pay_link(row) -> str:
    cfg = current_app.config
    q = [("pa", cfg["UPI_ID"]), ("pn", cfg["UPI_NAME"]), ("am", f"{row['amount'] / 100:.2f}"), ("cu", "INR"),
         ("tn", row["ref"])]
    if cfg.get("UPI_AID"):
        q.append(("aid", cfg["UPI_AID"]))
    return "upi://pay?" + urlencode(q, quote_via=quote, safe="@")


def _out(row, admin=False) -> dict:
    forever = row["period_days"] >= 3650
    out = {
        "ref": row["ref"], "plan_code": row["plan_code"], "plan_name": row["plan_name"],
        "period_days": row["period_days"], "forever": forever, "amount": row["amount"], "currency": "INR",
        "status": row["status"], "status_label": STATUS_LABEL.get(row["status"], row["status"]),
        "utr": row["utr"], "note": row["note"], "attempts_left": max(0, MAX_ATTEMPTS - row["attempts"]),
        "created_at": row["created_at"], "submitted_at": row["submitted_at"], "decided_at": row["decided_at"],
        "entitlement_until": row["entitlement_until"], "receipt_available": row["status"] == "approved",
        "user_name": row["user_name"],
    }
    if admin:                                   # an email address is only ever shown to administrators
        out["user_email"] = row["user_email"]
    out["upi_id"] = current_app.config["UPI_ID"]
    out["payee_name"] = current_app.config["UPI_NAME"]
    if row["status"] in ("awaiting_payment", "rejected"):
        out["pay_link"] = pay_link(row)
    return out


def _own(db, ref):
    user = current_user()
    q = "SELECT * FROM upi_payments WHERE ref = ?" + ("" if user["role"] == "admin" else " AND user_id = ?")
    return db.execute(q, (ref,) if user["role"] == "admin" else (ref, user["id"])).fetchone()


def _is_lifetime(user) -> bool:
    end = user["hosting_until"] if "hosting_until" in user.keys() else None
    return bool(end) and int(end[:4]) >= LIFETIME_YEAR


# ------------------------------------------------------------------ for the person paying

@bp.get("/api/upi")
@login_required
def home():
    user = current_user()
    rows = get_db().execute("SELECT * FROM upi_payments WHERE user_id = ? ORDER BY id DESC LIMIT 50",
                            (user["id"],)).fetchall()
    return jsonify(
        enabled=_enabled(),
        upi_id=current_app.config["UPI_ID"] if _enabled() else "",
        offers=[{"code": c, "name": p.get("name", c), "amount": p.get("amount"), "currency": "INR",
                 "period_days": p.get("period_days"), "forever": p.get("period_days", 0) >= 3650,
                 "description": p.get("description", "")} for c, p in offers().items()] if _enabled() else [],
        payments=[_out(r) for r in rows],
    )


@bp.post("/api/upi/orders")
@login_required
def create_order():
    if not _enabled():
        return jsonify(error="UPI payments are not available right now."), 503
    code = str((request.get_json(silent=True) or {}).get("plan", ""))
    offer = offers().get(code)
    if offer is None:
        return jsonify(error="Choose one of the plans on the Billing page."), 400
    user, db = current_user(), get_db()
    if _is_lifetime(user):
        return jsonify(error="You already have lifetime hosting. Nothing more to buy."), 409
    if rate_limit(db, f"upi:order:{user['id']}", limit=20, per_seconds=3600):
        return jsonify(error="Too many payment requests. Please try again later."), 429
    open_order = db.execute("SELECT * FROM upi_payments WHERE user_id = ? AND plan_code = ? "
                            "AND status = 'awaiting_payment' ORDER BY id DESC LIMIT 1", (user["id"], code)).fetchone()
    if open_order is not None:
        return jsonify(payment=_out(open_order))           # same plan, not paid yet: carry on with it
    db.execute("UPDATE upi_payments SET status = 'cancelled' WHERE user_id = ? AND status = 'awaiting_payment'",
               (user["id"],))                                # one open order at a time
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(5):
        ref = "CSK-" + "".join(secrets.choice(alphabet) for _ in range(8))
        try:
            db.execute("INSERT INTO upi_payments (ref, user_id, plan_code, plan_name, period_days, amount, user_name, "
                       "user_email, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (ref, user["id"], code, offer.get("name", code), int(offer["period_days"]), int(offer["amount"]),
                        user["name"], user["email"], utcnow()))
            break
        except sqlite3.IntegrityError:
            continue
    else:
        return jsonify(error="Could not create the payment request. Please try again."), 500
    db.commit()
    audit("upi.order", actor_id=user["id"], target=ref, ip=request.remote_addr, detail=code)
    row = db.execute("SELECT * FROM upi_payments WHERE ref = ?", (ref,)).fetchone()
    return jsonify(payment=_out(row)), 201


@bp.get("/api/upi/payments/<ref>")
@login_required
def get_payment(ref):
    row = _own(get_db(), ref)
    if row is None:
        return jsonify(error="Payment not found."), 404
    return jsonify(payment=_out(row))


@bp.post("/api/upi/payments/<ref>/submit")
@login_required
def submit(ref):
    db, user = get_db(), current_user()
    row = _own(db, ref)
    if row is None:
        return jsonify(error="Payment not found."), 404
    if rate_limit(db, f"upi:submit:{user['id']}", limit=10, per_seconds=3600):
        return jsonify(error="Too many attempts. Please try again later."), 429
    utr = re.sub(r"\s+", "", str((request.get_json(silent=True) or {}).get("utr", "")))
    if not UTR_RE.match(utr):
        return jsonify(error="Enter the 12-digit UPI transaction ID (also called UTR or reference number) "
                             "shown in your payment app."), 422
    if row["status"] == "submitted":
        return jsonify(error="Your transaction ID is already with us. We are verifying it."), 409
    if row["status"] == "approved":
        return jsonify(error="This payment is already approved."), 409
    if row["status"] not in ("awaiting_payment", "rejected"):
        return jsonify(error="This payment request is closed. Start a new one from the Billing page."), 409
    if row["attempts"] >= MAX_ATTEMPTS:
        return jsonify(error="Too many transaction IDs were tried for this payment. Please contact Codeskys support."), 429
    try:
        db.execute("UPDATE upi_payments SET utr = ?, status = 'submitted', attempts = attempts + 1, submitted_at = ?, "
                   "note = '' WHERE id = ? AND status IN ('awaiting_payment', 'rejected')", (utr, utcnow(), row["id"]))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify(error="That transaction ID has already been used for another payment."), 409
    audit("upi.submitted", actor_id=user["id"], target=ref, ip=request.remote_addr, detail=f"utr {utr}")
    _notify_admins(db, f"{row['user_name']} paid ₹{row['amount'] / 100:,.2f} for {row['plan_name']} "
                       f"(transaction {utr}). Check your bank app, then approve.")
    write_sheet(current_app)
    return jsonify(payment=_out(db.execute("SELECT * FROM upi_payments WHERE id = ?", (row["id"],)).fetchone()))


def _notify_admins(db, body):
    """Tell every administrator: in the bell, and by email when mail is set up (so a payment is not missed)."""
    from . import engine
    for a in db.execute("SELECT id, email FROM users WHERE role = 'admin'").fetchall():
        engine._notify(db, a["id"], "billing", "UPI payment to verify", body, "#/payments")
        _email(a["email"], "UPI payment to verify",
               body + "\n\nOpen Codeskys > Payments to approve it. Payments should be verified within 8 hours.")


def _email(to, subject, text):
    """Best-effort email: a mail problem must never break a payment."""
    try:
        from . import mailer
        mailer.send_mail(current_app, to, f"{current_app.config['PLATFORM_NAME']}: {subject}", text)
    except Exception as exc:
        current_app.logger.warning("Could not email %s: %s", to, exc)


# ------------------------------------------------------------------ receipt

RECEIPT_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Receipt {{ p.ref }}</title>
<style>body{font:15px/1.55 system-ui,Segoe UI,sans-serif;max-width:640px;margin:40px auto;padding:0 18px;color:#111}
h1{font-size:22px;margin:0}.sub{color:#555;margin:4px 0 22px}table{width:100%;border-collapse:collapse}
td{padding:9px 0;border-bottom:1px solid #e3e3e3}td:last-child{text-align:right;font-weight:600}
.badge{display:inline-block;background:#e7f8ef;color:#116b3c;padding:2px 10px;border-radius:99px;font-size:13px;font-weight:600}
small{color:#666}@media print{body{margin:0}}</style></head><body>
<h1>{{ platform }} payment receipt</h1><p class="sub">Receipt {{ p.ref }} &nbsp;<span class="badge">Paid</span></p>
<table>
<tr><td>Billed to</td><td>{{ p.user_name }}</td></tr>
<tr><td>Plan</td><td>{{ p.plan_name }}{% if not forever %} ({{ p.period_days }} days){% endif %}</td></tr>
<tr><td>Amount paid</td><td>INR {{ '%.2f'|format(p.amount / 100) }}</td></tr>
<tr><td>Paid to</td><td>{{ upi_id }}</td></tr>
<tr><td>UPI transaction ID</td><td>{{ p.utr }}</td></tr>
<tr><td>Approved on (IST)</td><td>{{ approved }}</td></tr>
<tr><td>Hosting active until</td><td>{{ until }}</td></tr>
</table>
<p><small>This is a payment receipt, not a GST tax invoice.</small></p></body></html>"""


@bp.get("/upi/receipts/<ref>")
@login_required
def receipt(ref):
    row = _own(get_db(), ref)
    if row is None or row["status"] != "approved":
        abort(404)
    forever = row["period_days"] >= 3650
    until = "Lifetime" if forever else _ist(row["entitlement_until"])
    return render_template_string(RECEIPT_HTML, p=row, forever=forever, until=until, approved=_ist(row["decided_at"]),
                                  upi_id=current_app.config["UPI_ID"], platform=current_app.config["PLATFORM_NAME"])


# ------------------------------------------------------------------ for the administrator

def _grant(db, row, user) -> str:
    """Paid hosting from today (or from the current end while it is still running)."""
    from . import engine
    now = datetime.now(timezone.utc)
    current_text = user["hosting_until"]
    current = datetime.fromisoformat(current_text) if current_text else None
    if current is not None and current.year >= LIFETIME_YEAR:
        until = current_text                          # already lifetime: never shorten it
    else:
        base = current if current is not None and current > now else now
        until = (base + timedelta(days=row["period_days"])).isoformat(timespec="seconds")
    db.execute("UPDATE users SET hosting_until = ? WHERE id = ?", (until, user["id"]))
    engine.extend_for_hosting(db, user["id"])         # live projects stay up: same URL, no rebuild
    return until


@bp.get("/api/admin/upi")
@admin_required
def admin_list():
    db = get_db()
    status = request.args.get("status", "submitted")
    where, args = ("", ())
    if status in STATUS_LABEL:
        where, args = " WHERE status = ?", (status,)
    rows = db.execute(f"SELECT * FROM upi_payments{where} ORDER BY id DESC LIMIT 500", args).fetchall()
    counts = {r["status"]: r["n"] for r in db.execute("SELECT status, COUNT(*) AS n FROM upi_payments GROUP BY status")}
    return jsonify(payments=[_out(r, admin=True) for r in rows], counts=counts,
                   upi_id=current_app.config["UPI_ID"])


@bp.post("/api/admin/upi/<ref>/approve")
@admin_required
def admin_approve(ref):
    db, admin = get_db(), current_user()
    row = db.execute("SELECT * FROM upi_payments WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        return jsonify(error="Payment not found."), 404
    claimed = db.execute("UPDATE upi_payments SET status = 'approved', decided_at = ?, decided_by = ? "
                         "WHERE id = ? AND status = 'submitted'", (utcnow(), admin["id"], row["id"])).rowcount
    if claimed != 1:
        db.rollback()
        return jsonify(error="This payment is not waiting for approval (it may already be decided)."), 409
    user = db.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
    until = _grant(db, row, user)
    db.execute("UPDATE upi_payments SET entitlement_until = ? WHERE id = ?", (until, row["id"]))
    db.commit()
    audit("upi.approved", actor_id=admin["id"], target=ref, ip=request.remote_addr,
          detail=f"{row['plan_code']} for {row['user_email']}, hosting until {until}")
    from . import engine
    done = ("Your payment was verified and your plan has been upgraded. Your projects now stay live "
            + ("for life." if row["period_days"] >= 3650 else f"until {until[:10]}."))
    engine._notify(db, user["id"], "billing", "Plan upgraded", done, "#/billing")
    _email(user["email"], "Your plan has been upgraded", done + f"\n\nReceipt: {row['ref']}")
    write_sheet(current_app)
    return jsonify(payment=_out(db.execute("SELECT * FROM upi_payments WHERE id = ?", (row["id"],)).fetchone(), True))


@bp.post("/api/admin/upi/<ref>/reject")
@admin_required
def admin_reject(ref):
    db, admin = get_db(), current_user()
    reason = str((request.get_json(silent=True) or {}).get("note", "")).strip()[:200]
    if len(reason) < 3:
        return jsonify(error="Write a short reason so the person knows what to fix."), 422
    row = db.execute("SELECT * FROM upi_payments WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        return jsonify(error="Payment not found."), 404
    changed = db.execute("UPDATE upi_payments SET status = 'rejected', note = ?, decided_at = ?, decided_by = ? "
                         "WHERE id = ? AND status = 'submitted'", (reason, utcnow(), admin["id"], row["id"])).rowcount
    if changed != 1:
        db.rollback()
        return jsonify(error="This payment is not waiting for approval (it may already be decided)."), 409
    db.commit()
    audit("upi.rejected", actor_id=admin["id"], target=ref, ip=request.remote_addr, detail=reason)
    from . import engine
    msg = f"We could not verify your payment: {reason} You can enter the transaction ID again."
    engine._notify(db, row["user_id"], "billing", "Payment could not be verified", msg, f"#/pay/{ref}")
    urow = db.execute("SELECT email FROM users WHERE id = ?", (row["user_id"],)).fetchone()
    if urow is not None:
        _email(urow["email"], "Your payment could not be verified", msg)
    write_sheet(current_app)
    return jsonify(payment=_out(db.execute("SELECT * FROM upi_payments WHERE id = ?", (row["id"],)).fetchone(), True))


# ------------------------------------------------------------------ the sheet (opens in Excel)

COLUMNS = ["Submitted (IST)", "Reference", "Name", "Email", "Plan", "Amount (INR)", "UPI transaction ID",
           "Status", "Decided (IST)", "Hosting until", "Note"]


def _ist(iso) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(IST).strftime("%d-%m-%Y %H:%M")
    except ValueError:
        return str(iso)


def _safe(value) -> str:
    """Stop a name like =HYPERLINK(...) from running as a formula when the sheet is opened in Excel."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def sheet_text(db) -> str:
    out = io.StringIO()
    out.write("\ufeff")                                     # lets Excel read UTF-8 (names, the rupee sign)
    w = csv.writer(out)
    w.writerow(COLUMNS)
    for r in db.execute("SELECT * FROM upi_payments WHERE status != 'awaiting_payment' AND status != 'cancelled' "
                        "ORDER BY id DESC"):
        until = "Lifetime" if r["period_days"] >= 3650 and r["status"] == "approved" else _ist(r["entitlement_until"])
        w.writerow([_ist(r["submitted_at"] or r["created_at"]), r["ref"], _safe(r["user_name"]), _safe(r["user_email"]),
                    _safe(r["plan_name"]), f"{r['amount'] / 100:.2f}", r["utr"] or "", STATUS_LABEL.get(r["status"], r["status"]),
                    _ist(r["decided_at"]), until if r["status"] == "approved" else "", _safe(r["note"])])
    return out.getvalue()


def write_sheet(app) -> None:
    """Keep ``instance/upi_payments.csv`` up to date after every change (best effort)."""
    try:
        from .db import connect
        db_path = Path(app.config["DATABASE"])
        conn = connect(str(db_path))
        try:
            text = sheet_text(conn)
        finally:
            conn.close()
        target = db_path.parent / "upi_payments.csv"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    except Exception as exc:                                # never let the sheet break a payment
        app.logger.warning("Could not update the payments sheet: %s", exc)


@bp.get("/api/admin/upi/export.csv")
@admin_required
def admin_export():
    audit("upi.export", actor_id=current_user()["id"], ip=request.remote_addr)
    return Response(sheet_text(get_db()), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=codeskys-payments.csv",
                             "Cache-Control": "no-store"})
