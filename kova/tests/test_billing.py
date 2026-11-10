"""Billing (Razorpay test mode) against a fake Razorpay API.

These prove the server-side rules: catalog pricing, signature checks, provider
confirmation, idempotency, replay protection, entitlement, renewal, refunds,
expiry and secret handling. They do not prove live Razorpay behaviour; that needs
real test-mode keys (see docs/BILLING.md).
"""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

from kova.billing import BillingConfig
from kova.db import connect

from .base import ADMIN, STUDENT, AppTestCase

KEY_ID, KEY_SECRET, WH_SECRET = "rzp_test_abc123", "test-key-secret-xyz", "test-webhook-secret-xyz"
OTHER = ("ola@example.test", "Ola Other", "other-pass-1234567")


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class FakeRazorpay:
    """Just enough of the Orders / Payments / Refunds API."""

    def __init__(self):
        self.orders, self.payments, self.calls = {}, {}, []
        self.down = False
        self.n = 0

    def request(self, method, url, auth=None, timeout=None, json=None):
        import requests
        self.calls.append((method, url, json))
        if self.down:
            raise requests.ConnectionError("down")
        assert auth == (KEY_ID, KEY_SECRET)
        path = url.split("/v1", 1)[1]
        self.n += 1
        if method == "POST" and path == "/orders":
            oid = f"order_{self.n:06d}"
            self.orders[oid] = dict(json, id=oid, status="created")
            return FakeResp(200, self.orders[oid])
        if method == "GET" and path.startswith("/payments/"):
            p = self.payments.get(path.split("/")[2])
            return FakeResp(200, p) if p else FakeResp(400, {"error": {}})
        if method == "POST" and path.endswith("/refund"):
            pid = path.split("/")[2]
            rid = f"rfnd_{self.n:06d}"
            return FakeResp(200, {"id": rid, "payment_id": pid, "amount": json["amount"], "status": "pending"})
        return FakeResp(404, {})

    def pay(self, order_id, status="captured", amount=None):
        o = self.orders[order_id]
        pid = f"pay_rzp{self.n:06d}"
        self.n += 1
        self.payments[pid] = {"id": pid, "order_id": order_id, "amount": amount or o["amount"],
                              "currency": o["currency"], "status": status}
        sig = hmac.new(KEY_SECRET.encode(), f"{order_id}|{pid}".encode(), hashlib.sha256).hexdigest()
        return pid, sig


class BillingTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.rzp = FakeRazorpay()
        self.configure()
        conn = connect(self.app.config["DATABASE"])
        from kova.users import create_user
        self.other_id = create_user(conn, *OTHER, role="student")
        conn.close()

    def configure(self, **env):
        base = {"RAZORPAY_KEY_ID": KEY_ID, "RAZORPAY_KEY_SECRET": KEY_SECRET,
                "RAZORPAY_WEBHOOK_SECRET": WH_SECRET, "KOVA_BILLING_CHECKOUT": "1"}
        base.update(env)
        self.app.config["_BILLING"] = BillingConfig({k: v for k, v in base.items() if v is not None})
        self.app.config["BILLING_HTTP"] = self.rzp

    # ---------------------------------------------------------------- helpers
    def user(self, uid):
        conn = connect(self.app.config["DATABASE"])
        try:
            return dict(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())
        finally:
            conn.close()

    def order(self, plan="student-6m", client=None):
        r = self.post("/api/billing/orders", {"plan": plan, "amount": 1}, client=client)
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def verify(self, order_id, pid, sig, client=None):
        return self.post("/api/billing/verify", {"razorpay_order_id": order_id, "razorpay_payment_id": pid,
                                                 "razorpay_signature": sig}, client=client)

    def hook(self, event, entity_key, entity, event_id=None, secret=WH_SECRET, created=None):
        body = json.dumps({"event": event, "created_at": created or int(time.time()),
                           "payload": {entity_key: {"entity": entity}}}).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {"X-Razorpay-Signature": sig, "Content-Type": "application/json"}
        if event_id:
            headers["X-Razorpay-Event-Id"] = event_id
        return self.app.test_client().post("/api/hooks/razorpay", data=body, headers=headers)

    def days_left(self, uid):
        exp = datetime.fromisoformat(self.user(uid)["plan_expires_at"])
        return (exp - datetime.now(timezone.utc)).total_seconds() / 86400

    def as_student(self):
        self.login(STUDENT[0], STUDENT[2])

    # ---------------------------------------------------------------- configuration
    def test_not_configured_sells_nothing(self):
        self.configure(RAZORPAY_KEY_ID=None, RAZORPAY_KEY_SECRET=None)
        self.as_student()
        d = self.client.get("/api/billing").get_json()
        self.assertFalse(d["enabled"])
        self.assertEqual(d["catalog"], [])
        self.assertEqual(self.post("/api/billing/orders", {"plan": "student-6m"}).status_code, 503)
        self.assertEqual(self.rzp.calls, [])

    def test_checkout_closed_until_owner_opens_it(self):
        self.configure(KOVA_BILLING_CHECKOUT=None)
        self.as_student()
        d = self.client.get("/api/billing").get_json()
        self.assertTrue(d["enabled"])
        self.assertFalse(d["checkout_open"])
        r = self.post("/api/billing/orders", {"plan": "student-6m"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.rzp.calls, [])                       # provider never contacted
        self.assertNotIn("razorpay", self.client.get("/").headers["Content-Security-Policy"])
        self.login(ADMIN[0], ADMIN[2])
        self.assertTrue(any("KOVA_BILLING_CHECKOUT" in p for p in self.client.get("/api/admin/billing").get_json()["problems"]))

    def test_default_catalog_is_student_500_for_6_months_only(self):
        cat = self.app.config["_BILLING"].catalog
        self.assertEqual(list(cat), ["student-6m"])                 # Premium is not sellable
        self.assertEqual((cat["student-6m"]["amount"], cat["student-6m"]["period_days"]), (50000, 180))

    def test_live_keys_refused_without_explicit_opt_in(self):
        self.configure(RAZORPAY_KEY_ID="rzp_live_x")
        cfg = self.app.config["_BILLING"]
        self.assertFalse(cfg.enabled)
        self.assertTrue(any("RAZORPAY_ALLOW_LIVE" in p for p in cfg.problems()))
        self.configure(RAZORPAY_KEY_ID="rzp_live_x", RAZORPAY_ALLOW_LIVE="1")
        self.assertTrue(self.app.config["_BILLING"].enabled)

    def test_bad_catalog_disables_billing(self):
        self.configure(KOVA_BILLING_PLANS_JSON=json.dumps({"x": {"amount": 5, "currency": "USD",
                                                                 "grants_plan": "hobby", "period_days": 0}}))
        problems = self.app.config["_BILLING"].problems()
        self.assertEqual(len(problems), 4, problems)

    def test_secrets_never_in_responses(self):
        self.as_student()
        o = self.order()
        pid, sig = self.rzp.pay(o["order_id"])
        bodies = [self.client.get("/api/billing").get_data(as_text=True), json.dumps(o),
                  self.verify(o["order_id"], pid, sig).get_data(as_text=True)]
        self.login(ADMIN[0], ADMIN[2])
        bodies.append(self.client.get("/api/admin/billing").get_data(as_text=True))
        for b in bodies:
            self.assertNotIn(KEY_SECRET, b)
            self.assertNotIn(WH_SECRET, b)

    def test_csp_allows_checkout_only_when_enabled_and_signed_in(self):
        self.assertNotIn("razorpay", self.client.get("/").headers["Content-Security-Policy"])
        self.as_student()
        self.assertIn("https://checkout.razorpay.com", self.client.get("/").headers["Content-Security-Policy"])
        self.assertNotIn("razorpay", self.client.get("/api/billing").headers["Content-Security-Policy"])
        self.configure(RAZORPAY_KEY_ID=None)
        self.assertNotIn("razorpay", self.client.get("/").headers["Content-Security-Policy"])

    # ---------------------------------------------------------------- checkout
    def test_order_amount_comes_from_server_catalog(self):
        self.as_student()
        o = self.order()
        self.assertEqual((o["amount"], o["currency"]), (50000, "INR"))
        self.assertEqual(self.rzp.calls[-1][2]["amount"], 50000)
        self.assertEqual(self.post("/api/billing/orders", {"plan": "free-stuff"}).status_code, 422)

    def test_order_requires_login_and_csrf(self):
        self.assertEqual(self.post("/api/billing/orders", {"plan": "student-6m"}).status_code, 401)
        self.as_student()
        r = self.post("/api/billing/orders", {"plan": "student-6m"}, token="wrong")
        self.assertEqual(r.status_code, 403)

    def test_successful_payment_grants_plan_once(self):
        self.as_student()
        o = self.order()
        pid, sig = self.rzp.pay(o["order_id"])
        r = self.verify(o["order_id"], pid, sig)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["outcome"], "paid")
        self.assertEqual(self.user(self.student_id)["plan"], "pro")
        self.assertAlmostEqual(self.days_left(self.student_id), 180, delta=0.01)
        # verify again + the webhook: no second extension
        self.verify(o["order_id"], pid, sig)
        self.hook("payment.captured", "payment", self.rzp.payments[pid], event_id="evt_1")
        self.assertAlmostEqual(self.days_left(self.student_id), 180, delta=0.01)
        hist = self.client.get("/api/billing").get_json()["payments"]
        self.assertEqual([p["status"] for p in hist], ["paid"])

    def test_forged_signature_grants_nothing(self):
        self.as_student()
        o = self.order()
        pid, _ = self.rzp.pay(o["order_id"])
        r = self.verify(o["order_id"], pid, "0" * 64)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.user(self.student_id)["plan"], "hobby")

    def test_cannot_verify_another_users_order(self):
        self.as_student()
        o = self.order()
        pid, sig = self.rzp.pay(o["order_id"])
        other = self.app.test_client()
        self.login(OTHER[0], OTHER[2], client=other)
        self.assertEqual(self.verify(o["order_id"], pid, sig, client=other).status_code, 400)
        self.assertEqual(self.user(self.other_id)["plan"], "hobby")

    def test_payment_from_another_order_is_rejected(self):
        self.as_student()
        o1, o2 = self.order(), self.order()
        pid, _ = self.rzp.pay(o1["order_id"])
        # signature computed for o2|pid but the provider says pid belongs to o1
        sig = hmac.new(KEY_SECRET.encode(), f"{o2['order_id']}|{pid}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(self.verify(o2["order_id"], pid, sig).status_code, 400)
        self.assertEqual(self.user(self.student_id)["plan"], "hobby")

    def test_authorized_payment_is_pending_until_captured_webhook(self):
        self.as_student()
        o = self.order()
        pid, sig = self.rzp.pay(o["order_id"], status="authorized")
        self.assertEqual(self.verify(o["order_id"], pid, sig).get_json()["outcome"], "pending")
        self.assertEqual(self.user(self.student_id)["plan"], "hobby")
        cap = dict(self.rzp.payments[pid], status="captured")
        self.assertEqual(self.hook("payment.captured", "payment", cap).get_json()["outcome"], "paid")
        self.assertEqual(self.user(self.student_id)["plan"], "pro")

    def test_provider_unreachable_at_verify_stays_pending(self):
        self.as_student()
        o = self.order()
        pid, sig = self.rzp.pay(o["order_id"])
        self.rzp.down = True
        self.assertEqual(self.verify(o["order_id"], pid, sig).get_json()["outcome"], "pending")
        self.assertEqual(self.user(self.student_id)["plan"], "hobby")   # never on signature alone
        self.hook("payment.captured", "payment", self.rzp.payments[pid])
        self.assertEqual(self.user(self.student_id)["plan"], "pro")

    def test_failed_then_successful_attempt(self):
        self.as_student()
        o = self.order()
        bad, _ = self.rzp.pay(o["order_id"], status="failed")
        self.hook("payment.failed", "payment", dict(self.rzp.payments[bad], error_description="card declined"))
        h = self.client.get("/api/billing").get_json()["payments"][0]
        self.assertEqual((h["status"], h["failure_reason"]), ("failed", "card declined"))
        good, sig = self.rzp.pay(o["order_id"])
        self.assertEqual(self.verify(o["order_id"], good, sig).get_json()["outcome"], "paid")
        # a late failure event for the old attempt cannot undo the payment
        self.hook("payment.failed", "payment", self.rzp.payments[bad], event_id="late")
        self.assertEqual(self.client.get("/api/billing").get_json()["payments"][0]["status"], "paid")

    def test_renewal_extends_from_current_expiry(self):
        self.as_student()
        for _ in range(2):
            o = self.order()
            pid, sig = self.rzp.pay(o["order_id"])
            self.verify(o["order_id"], pid, sig)
        self.assertAlmostEqual(self.days_left(self.student_id), 360, delta=0.01)

    def test_admin_managed_plan_cannot_be_bought_over(self):
        conn = connect(self.app.config["DATABASE"])
        conn.execute("UPDATE users SET plan = 'team' WHERE id = ?", (self.student_id,))
        conn.commit(); conn.close()
        self.as_student()
        self.assertEqual(self.post("/api/billing/orders", {"plan": "student-6m"}).status_code, 409)

    # ---------------------------------------------------------------- webhook
    def test_webhook_rejects_bad_signature(self):
        r = self.hook("payment.captured", "payment", {"order_id": "x"}, secret="wrong")
        self.assertEqual(r.status_code, 400)

    def test_webhook_replay_with_new_event_id_is_ignored(self):
        self.as_student()
        o = self.order()
        pid, _ = self.rzp.pay(o["order_id"])
        ent = self.rzp.payments[pid]
        body = json.dumps({"event": "payment.captured", "created_at": int(time.time()),
                           "payload": {"payment": {"entity": ent}}}).encode()
        sig = hmac.new(WH_SECRET.encode(), body, hashlib.sha256).hexdigest()
        c = self.app.test_client()
        r1 = c.post("/api/hooks/razorpay", data=body, headers={"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": "a"})
        r2 = c.post("/api/hooks/razorpay", data=body, headers={"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": "b"})
        self.assertEqual(r1.get_json().get("outcome"), "paid")
        self.assertTrue(r2.get_json().get("duplicate"))
        self.assertAlmostEqual(self.days_left(self.student_id), 180, delta=0.01)

    def test_webhook_amount_mismatch_grants_nothing(self):
        self.as_student()
        o = self.order()
        pid, _ = self.rzp.pay(o["order_id"], amount=100)
        self.assertEqual(self.hook("payment.captured", "payment", self.rzp.payments[pid]).get_json()["outcome"], "mismatch")
        self.assertEqual(self.user(self.student_id)["plan"], "hobby")

    def test_stale_webhook_ignored(self):
        self.as_student()
        o = self.order()
        pid, _ = self.rzp.pay(o["order_id"])
        r = self.hook("payment.captured", "payment", self.rzp.payments[pid], created=int(time.time()) - 4 * 86400)
        self.assertEqual(r.get_json()["ignored"], "stale")
        self.assertEqual(self.user(self.student_id)["plan"], "hobby")

    # ---------------------------------------------------------------- refunds
    def _paid(self):
        self.as_student()
        o = self.order()
        pid, sig = self.rzp.pay(o["order_id"])
        self.verify(o["order_id"], pid, sig)
        return o["ref"], pid

    def test_full_refund_revokes_access(self):
        ref, pid = self._paid()
        self.assertEqual(self.post(f"/api/admin/billing/payments/{ref}/refund").status_code, 403)  # student
        self.login(ADMIN[0], ADMIN[2])
        r = self.post(f"/api/admin/billing/payments/{ref}/refund")
        self.assertEqual(r.status_code, 200, r.get_json())
        rid = r.get_json()["refund"]["id"]
        self.assertEqual(self.user(self.student_id)["plan"], "pro")   # pending refund: unchanged
        self.hook("refund.processed", "refund", {"id": rid, "payment_id": pid, "amount": 50000, "status": "processed"})
        u = self.user(self.student_id)
        self.assertEqual((u["plan"], u["plan_expires_at"]), ("hobby", None))
        pay = [p for p in self.client.get("/api/admin/billing").get_json()["payments"] if p["ref"] == ref][0]
        self.assertEqual((pay["status"], pay["refunded_amount"]), ("refunded", 50000))

    def test_partial_refund_keeps_access(self):
        ref, pid = self._paid()
        self.login(ADMIN[0], ADMIN[2])
        r = self.post(f"/api/admin/billing/payments/{ref}/refund", {"amount": 10000})
        rid = r.get_json()["refund"]["id"]
        self.hook("refund.processed", "refund", {"id": rid, "payment_id": pid, "amount": 10000, "status": "processed"})
        self.assertEqual(self.user(self.student_id)["plan"], "pro")
        self.assertEqual(self.post(f"/api/admin/billing/payments/{ref}/refund", {"amount": 99999}).status_code, 422)

    # ---------------------------------------------------------------- expiry, receipts
    def test_expired_plan_falls_back_to_hobby_limits(self):
        self._paid()
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
        conn = connect(self.app.config["DATABASE"])
        conn.execute("UPDATE users SET plan_expires_at = ? WHERE id = ?", (past, self.student_id))
        conn.commit(); conn.close()
        me = self.client.get("/api/auth/me").get_json()["user"]
        self.assertEqual((me["plan"], me["plan_expires_at"]), ("hobby", None))

    def test_receipts_are_private_and_only_for_paid(self):
        self.as_student()
        unpaid = self.order()
        self.assertEqual(self.client.get(f"/billing/receipts/{unpaid['ref']}").status_code, 404)
        o = self.order()
        pid, sig = self.rzp.pay(o["order_id"])
        self.verify(o["order_id"], pid, sig)
        r = self.client.get(f"/billing/receipts/{o['ref']}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("TEST MODE", r.get_data(as_text=True))
        self.assertIn("INR 500.00", r.get_data(as_text=True))
        other = self.app.test_client()
        self.login(OTHER[0], OTHER[2], client=other)
        self.assertEqual(other.get(f"/billing/receipts/{o['ref']}").status_code, 404)
