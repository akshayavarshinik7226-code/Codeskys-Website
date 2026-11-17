"""Paid hosting (₹510 for 30 days): a paid student's deployments outlive the free 24 hours.

Payments go through the real verify/webhook code against the fake Razorpay of test_billing; the
deployment is a simulator one, so expiry runs without a deployment engine.
"""
import secrets
from datetime import datetime, timedelta, timezone

from kova import engine, jobs
from kova.db import connect

from .base import STUDENT, AppTestCase
from . import test_billing as billing    # the module, so its test class is not collected again here

URL = "https://web-app.apps.example.com"


def iso(dt):
    return dt.isoformat(timespec="seconds")


class HostingTests(AppTestCase):
    # The billing test helpers (fake Razorpay, orders, verify, webhooks), not the billing tests themselves.
    B = billing.BillingTests
    configure, user, order, verify, hook, as_student = B.configure, B.user, B.order, B.verify, B.hook, B.as_student

    def setUp(self):
        super().setUp()
        self.rzp = billing.FakeRazorpay()
        self.configure()
        self.as_student()
        conn = self.db()
        self.uid = conn.execute("SELECT id FROM users WHERE email = ?", (STUDENT[0],)).fetchone()["id"]
        now = datetime.now(timezone.utc)
        conn.execute("INSERT INTO projects (owner_id, name, slug, repo_url, branch, framework, port, provider, hook_token, "
                     "created_at, updated_at) VALUES (?, 'Web App', 'web-app', 'https://github.com/acme/web-app', 'main', "
                     "'static', 80, 'simulator', ?, ?, ?)", (self.uid, secrets.token_urlsafe(16), iso(now), iso(now)))
        self.pid = conn.execute("SELECT id FROM projects WHERE slug = 'web-app'").fetchone()["id"]
        conn.execute("INSERT INTO deployments (project_id, number, status, stage, target, branch, commit_sha, trigger, provider, "
                     "url, created_at, started_at, finished_at, expires_at) VALUES (?, 1, 'ready', 'ready', 'production', "
                     "'main', ?, 'initial', 'simulator', ?, ?, ?, ?, ?)",
                     (self.pid, "a" * 40, URL, iso(now), iso(now), iso(now), iso(now + timedelta(seconds=60))))
        self.dep = conn.execute("SELECT id FROM deployments").fetchone()["id"]
        conn.execute("UPDATE projects SET current_deployment_id = ? WHERE id = ?", (self.dep, self.pid))
        conn.commit()
        conn.close()

    def db(self):
        return connect(self.app.config["DATABASE"])

    def deployment(self):
        conn = self.db()
        try:
            return dict(conn.execute("SELECT * FROM deployments WHERE id = ?", (self.dep,)).fetchone())
        finally:
            conn.close()

    def pay_hosting(self):
        o = self.order("hosting-30d")
        self.assertEqual(o["amount"], 51000)
        pid, sig = self.rzp.pay(o["order_id"])
        r = self.verify(o["order_id"], pid, sig)
        self.assertEqual(r.status_code, 200, r.get_json())
        return o

    def expire_now(self, **payload):
        conn = self.db()
        try:
            engine.expire_deployment(conn, self.app.config, {"deployment_id": self.dep, **payload}, lambda *a, **k: True)
        finally:
            conn.close()

    def age(self, seconds):
        conn = self.db()
        conn.execute("UPDATE deployments SET expires_at = ? WHERE id = ?",
                     (iso(datetime.now(timezone.utc) - timedelta(seconds=seconds)), self.dep))
        conn.commit()
        conn.close()

    # ---------------------------------------------------------------- the paid upgrade
    def test_a_verified_payment_keeps_the_same_deployment_live_for_30_days(self):
        before = self.deployment()
        self.pay_hosting()
        user, dep = self.user(self.uid), self.deployment()
        until = datetime.fromisoformat(user["hosting_until"])
        self.assertAlmostEqual((until - datetime.now(timezone.utc)).total_seconds() / 86400, 30, delta=0.01)
        self.assertEqual(dep["expires_at"], iso(until + timedelta(hours=24)))   # end of the period + grace
        self.assertEqual((dep["id"], dep["url"], dep["status"], dep["commit_sha"]),
                         (before["id"], before["url"], "ready", before["commit_sha"]))   # no rebuild, same URL
        self.assertEqual(user["plan"], "hobby")                                       # the plan is unchanged
        account = self.client.get("/api/billing").get_json()["account"]
        self.assertEqual(account["hosting_until"], user["hosting_until"])
        project = self.client.get("/api/projects/web-app").get_json()["project"]
        self.assertEqual(project["hosting_until"], user["hosting_until"])

    def test_a_success_message_from_the_browser_is_not_enough(self):
        o = self.order("hosting-30d")
        pid, sig = self.rzp.pay(o["order_id"])
        self.assertEqual(self.verify(o["order_id"], pid, "0" * 64).status_code, 400)   # forged signature
        o2 = self.order("hosting-30d")
        pid2, sig2 = self.rzp.pay(o2["order_id"], status="authorized")                # not captured
        self.verify(o2["order_id"], pid2, sig2)
        o3 = self.order("hosting-30d")
        pid3, sig3 = self.rzp.pay(o3["order_id"], amount=100)                         # wrong amount
        self.verify(o3["order_id"], pid3, sig3)
        self.assertIsNone(self.user(self.uid)["hosting_until"])
        self.assertEqual(self.deployment()["expires_at"][:10], self.deployment()["finished_at"][:10])

    def test_any_plan_can_buy_hosting(self):
        conn = self.db()
        conn.execute("UPDATE users SET plan = 'pro', plan_expires_at = ? WHERE id = ?",
                     (iso(datetime.now(timezone.utc) + timedelta(days=100)), self.uid))
        conn.commit()
        conn.close()
        self.pay_hosting()
        self.assertEqual(self.user(self.uid)["plan"], "pro")
        self.assertIsNotNone(self.user(self.uid)["hosting_until"])

    def test_renewing_adds_30_days_to_the_current_end(self):
        self.pay_hosting()
        first = datetime.fromisoformat(self.user(self.uid)["hosting_until"])
        self.pay_hosting()
        self.assertEqual(datetime.fromisoformat(self.user(self.uid)["hosting_until"]), first + timedelta(days=30))

    # ---------------------------------------------------------------- expiry protection
    def test_the_expiry_worker_never_removes_a_paid_deployment(self):
        self.pay_hosting()
        self.age(5)                                    # as if the old 60-second expiry had been reached
        conn = self.db()
        jobs.sweep(conn, self.app.config)
        conn.close()
        self.expire_now()                              # the queued job runs anyway
        dep = self.deployment()
        self.assertEqual((dep["status"], dep["expired_at"]), ("ready", None))
        self.assertGreater(dep["expires_at"], iso(datetime.now(timezone.utc) + timedelta(days=29)))

    def test_an_unpaid_deployment_expires(self):
        self.age(5)
        self.expire_now()
        self.assertEqual(self.deployment()["status"], "expired")

    def test_payment_after_the_expiry_started_does_not_resurrect_it(self):
        conn = self.db()
        conn.execute("UPDATE deployments SET stage = 'expiring' WHERE id = ?", (self.dep,))   # the claim won
        conn.commit()
        conn.close()
        self.pay_hosting()
        self.assertIsNotNone(self.user(self.uid)["hosting_until"])                # the payment still counts
        self.expire_now()
        self.assertEqual(self.deployment()["status"], "expired")                  # it was already being removed

    def test_payment_just_before_the_expiry_job_wins(self):
        self.age(5)
        conn = self.db()
        jobs.sweep(conn, self.app.config)              # expiry queued...
        conn.close()
        self.pay_hosting()                             # ...the payment lands before the job runs
        self.expire_now()
        self.assertEqual(self.deployment()["status"], "ready")

    def test_an_administrator_can_still_expire_a_paid_deployment(self):
        self.pay_hosting()
        self.expire_now(force=True)
        self.assertEqual(self.deployment()["status"], "expired")

    # ---------------------------------------------------------------- after the paid period
    def test_after_the_period_a_reminder_then_the_normal_expiry(self):
        self.pay_hosting()
        ended = datetime.now(timezone.utc) - timedelta(hours=1)
        conn = self.db()
        conn.execute("UPDATE users SET hosting_until = ? WHERE id = ?", (iso(ended), self.uid))
        conn.execute("UPDATE deployments SET expires_at = ? WHERE id = ?", (iso(ended + timedelta(hours=24)), self.dep))
        conn.commit()
        jobs.sweep(conn, self.app.config)
        jobs.sweep(conn, self.app.config)
        notes = [r["title"] for r in conn.execute("SELECT title FROM notifications WHERE user_id = ?", (self.uid,))]
        conn.close()
        self.assertEqual(notes.count("Paid hosting ended"), 1)                   # once, during the grace
        self.assertEqual(self.deployment()["status"], "ready")                   # still in the 24 h grace
        conn = self.db()
        conn.execute("UPDATE users SET hosting_until = ? WHERE id = ?", (iso(ended - timedelta(hours=24)), self.uid))
        conn.commit()
        conn.close()
        self.age(5)
        self.expire_now()
        self.assertEqual(self.deployment()["status"], "expired")                 # grace over: expires as usual

    def test_a_full_refund_takes_the_period_back(self):
        o = self.pay_hosting()
        conn = self.db()
        pay = conn.execute("SELECT * FROM payments WHERE razorpay_order_id = ?", (o["order_id"],)).fetchone()
        conn.close()
        r = self.hook("refund.processed", "refund", {"id": "rfnd_1", "payment_id": pay["razorpay_payment_id"],
                                                      "amount": 51000, "status": "processed"}, event_id="evt_r1")
        self.assertEqual(r.status_code, 200, r.get_json())
        dep = self.deployment()
        left = (datetime.fromisoformat(dep["expires_at"]) - datetime.now(timezone.utc)).total_seconds() / 3600
        self.assertAlmostEqual(left, 24, delta=0.1)                              # the grace, then normal expiry

    def test_a_new_deployment_of_a_paid_account_gets_the_paid_lifetime(self):
        self.pay_hosting()
        conn = self.db()
        until = engine.hosting_expiry(conn, self.uid)
        conn.close()
        self.assertGreater(until, iso(datetime.now(timezone.utc) + timedelta(days=30)))

