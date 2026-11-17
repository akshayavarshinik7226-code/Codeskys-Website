"""Manual UPI payments: order -> receipt page -> transaction ID -> administrator approval -> sheet."""
import io
import os
import zipfile
from datetime import datetime, timezone

from kova.db import connect
from kova.users import create_user
from .base import ADMIN, STUDENT, AppTestCase
from .test_platform import PlatformCase


def _zip():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("index.html", "<h1>hi</h1>")
    b.seek(0)
    return b


class UpiBase(PlatformCase):
    UTR = "412345678901"

    def student(self):
        self.as_student()

    def admin_client(self):
        c = self.app.test_client()
        self.login(ADMIN[0], ADMIN[2], client=c)
        return c

    def order(self, plan="hosting-6m", client=None):
        r = self.post("/api/upi/orders", {"plan": plan}, client=client)
        self.assertIn(r.status_code, (200, 201), r.get_json())
        return r.get_json()["payment"]

    def submit(self, ref, utr=None, client=None):
        return self.post(f"/api/upi/payments/{ref}/submit", {"utr": self.UTR if utr is None else utr}, client=client)

    def decide(self, ref, action, payload=None, admin=None):
        admin = admin or self.admin_client()
        return self.post(f"/api/admin/upi/{ref}/{action}", payload or {}, client=admin)

    def user_row(self, uid=None):
        conn = connect(self.app.config["DATABASE"])
        try:
            return dict(conn.execute("SELECT * FROM users WHERE id = ?", (uid or self.student_id,)).fetchone())
        finally:
            conn.close()


class UpiOrderTests(UpiBase):
    def test_order_gives_a_receipt_with_the_exact_upi_link(self):
        self.student()
        p = self.order()
        self.assertEqual((p["amount"], p["status"], p["period_days"]), (51000, "awaiting_payment", 180))
        self.assertTrue(p["ref"].startswith("CSK-"))
        link = p["pay_link"]
        self.assertTrue(link.startswith("upi://pay?pa=codeskysindia@okhdfcbank&pn=Codeskys&am=510.00&cu=INR"), link)
        self.assertIn(f"tn={p['ref']}", link)
        self.assertIn("aid=uGICAgKCg58W_aw", link)
        self.assertEqual(p["user_name"], STUDENT[1])           # details come from the account, not typed

    def test_lifetime_amount_is_1599(self):
        self.student()
        p = self.order("hosting-lifetime")
        self.assertEqual(p["amount"], 159900)
        self.assertIn("am=1599.00", p["pay_link"])

    def test_the_same_plan_reuses_the_open_order_and_other_plans_replace_it(self):
        self.student()
        a = self.order()
        self.assertEqual(self.order()["ref"], a["ref"])
        b = self.order("hosting-lifetime")
        self.assertNotEqual(b["ref"], a["ref"])
        self.assertEqual(self.client.get(f"/api/upi/payments/{a['ref']}").get_json()["payment"]["status"], "cancelled")

    def test_unknown_plans_and_signed_out_requests_are_refused(self):
        self.student()
        self.assertEqual(self.post("/api/upi/orders", {"plan": "free-gold"}).status_code, 400)
        self.assertEqual(self.post("/api/upi/orders", {}).status_code, 400)
        self.assertEqual(self.app.test_client().get("/api/upi").status_code, 401)

    def test_upi_can_be_switched_off(self):
        self.student()
        self.app.config["UPI_ENABLED"] = False
        self.assertEqual(self.post("/api/upi/orders", {"plan": "hosting-6m"}).status_code, 503)
        self.assertFalse(self.client.get("/api/upi").get_json()["enabled"])


class UpiSubmitTests(UpiBase):
    def test_the_transaction_id_must_be_12_digits(self):
        self.student()
        ref = self.order()["ref"]
        for bad in ("", "12345", "41234567890", "4123456789012", "41234567890a", "abcdefghijkl"):
            self.assertEqual(self.submit(ref, bad).status_code, 422, bad)
        r = self.submit(ref, "4123 4567 8901")                  # spaces from copy-paste are fine
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["payment"]["status"], "submitted")
        self.assertEqual(r.get_json()["payment"]["utr"], self.UTR)

    def test_nothing_is_granted_until_an_administrator_approves(self):
        self.student()
        ref = self.order()["ref"]
        self.submit(ref)
        self.assertIsNone(self.user_row()["hosting_until"])
        self.assertEqual(self.submit(ref).status_code, 409)     # already with us

    def test_one_transaction_id_cannot_be_used_twice(self):
        self.student()
        ref = self.order()["ref"]
        self.submit(ref)
        conn = connect(self.app.config["DATABASE"])
        other = create_user(conn, "eve@gmail.com", "Eve", "Maple-River-Lamp-47!", "student"); conn.close()
        eve = self.app.test_client()
        self.login("eve@gmail.com", "Maple-River-Lamp-47!", client=eve)
        ref2 = self.order(client=eve)["ref"]
        r = self.submit(ref2, client=eve)
        self.assertEqual(r.status_code, 409)
        self.assertIn("already been used", r.get_json()["error"])

    def test_nobody_can_see_or_submit_someone_elses_payment(self):
        self.student()
        ref = self.order()["ref"]
        conn = connect(self.app.config["DATABASE"])
        create_user(conn, "eve@gmail.com", "Eve", "Maple-River-Lamp-47!", "student"); conn.close()
        eve = self.app.test_client()
        self.login("eve@gmail.com", "Maple-River-Lamp-47!", client=eve)
        self.assertEqual(eve.get(f"/api/upi/payments/{ref}").status_code, 404)
        self.assertEqual(self.submit(ref, client=eve).status_code, 404)


class UpiApprovalTests(UpiBase):
    def test_students_cannot_use_the_admin_endpoints(self):
        self.student()
        ref = self.order()["ref"]
        self.submit(ref)
        self.assertEqual(self.client.get("/api/admin/upi").status_code, 403)
        self.assertEqual(self.post(f"/api/admin/upi/{ref}/approve").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/upi/export.csv").status_code, 403)

    def test_approving_gives_180_days_and_extends_a_live_project(self):
        self.student()
        r = self.client.post("/api/projects/upload", data={"name": "Live One", "file": (_zip(), "s.zip")},
                             content_type="multipart/form-data", headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.work()
        before = self.client.get("/api/projects/live-one").get_json()["project"]["production"]["expires_at"]
        ref = self.order()["ref"]
        self.submit(ref)
        r = self.decide(ref, "approve")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["payment"]["status"], "approved")
        left = (datetime.fromisoformat(self.user_row()["hosting_until"]) - datetime.now(timezone.utc)).days
        self.assertTrue(179 <= left <= 180, left)
        after = self.client.get("/api/projects/live-one").get_json()["project"]["production"]["expires_at"]
        self.assertGreater(after, before)                       # the free 24 hours became paid hosting
        self.assertEqual(self.client.get("/api/upi").get_json()["payments"][0]["status"], "approved")

    def test_approving_twice_does_not_grant_twice(self):
        self.student()
        ref = self.order()["ref"]
        self.submit(ref)
        a = self.admin_client()
        self.assertEqual(self.decide(ref, "approve", admin=a).status_code, 200)
        first = self.user_row()["hosting_until"]
        self.assertEqual(self.decide(ref, "approve", admin=a).status_code, 409)
        self.assertEqual(self.user_row()["hosting_until"], first)

    def test_a_second_payment_adds_to_the_current_end(self):
        self.student()
        for utr in ("111111111111", "222222222222"):
            ref = self.order()["ref"]
            self.submit(ref, utr)
            self.decide(ref, "approve")
        left = (datetime.fromisoformat(self.user_row()["hosting_until"]) - datetime.now(timezone.utc)).days
        self.assertTrue(358 <= left <= 360, left)

    def test_lifetime_is_a_hosting_date_a_century_away_and_blocks_more_orders(self):
        self.student()
        ref = self.order("hosting-lifetime")["ref"]
        self.submit(ref)
        self.decide(ref, "approve")
        self.assertGreaterEqual(int(self.user_row()["hosting_until"][:4]), 2100)
        self.assertEqual(self.post("/api/upi/orders", {"plan": "hosting-6m"}).status_code, 409)

    def test_rejecting_needs_a_reason_and_lets_the_person_try_again_three_times(self):
        self.student()
        ref = self.order()["ref"]
        a = self.admin_client()
        self.submit(ref, "111111111111")
        self.assertEqual(self.decide(ref, "reject", {"note": ""}, admin=a).status_code, 422)
        r = self.decide(ref, "reject", {"note": "Amount not received"}, admin=a)
        self.assertEqual(r.get_json()["payment"]["status"], "rejected")
        self.assertIsNone(self.user_row()["hosting_until"])
        shown = self.client.get(f"/api/upi/payments/{ref}").get_json()["payment"]
        self.assertEqual((shown["note"], shown["attempts_left"]), ("Amount not received", 2))
        self.assertIn("pay_link", shown)                         # can pay/enter again
        self.submit(ref, "333333333333")                         # corrected id, same order
        self.decide(ref, "reject", {"note": "Still not found"}, admin=a)
        self.submit(ref, "444444444444")
        self.decide(ref, "reject", {"note": "Not found"}, admin=a)
        self.assertEqual(self.submit(ref, "555555555555").status_code, 429)   # three tries used

    def test_a_rejected_id_can_be_reused_by_the_right_payer(self):
        self.student()
        ref = self.order()["ref"]
        self.submit(ref)
        self.decide(ref, "reject", {"note": "Wrong person"})
        conn = connect(self.app.config["DATABASE"])
        create_user(conn, "eve@gmail.com", "Eve", "Maple-River-Lamp-47!", "student"); conn.close()
        eve = self.app.test_client()
        self.login("eve@gmail.com", "Maple-River-Lamp-47!", client=eve)
        self.assertEqual(self.submit(self.order(client=eve)["ref"], client=eve).status_code, 200)

    def test_admin_list_counts_and_filters(self):
        self.student()
        ref = self.order()["ref"]
        self.submit(ref)
        a = self.admin_client()
        data = a.get("/api/admin/upi?status=submitted").get_json()
        self.assertEqual([p["ref"] for p in data["payments"]], [ref])
        self.assertEqual(data["counts"].get("submitted"), 1)
        self.assertEqual(a.get("/api/admin/upi?status=approved").get_json()["payments"], [])


class UpiSheetAndReceiptTests(UpiBase):
    def test_every_submission_lands_in_the_sheet_with_account_details(self):
        self.student()
        ref = self.order()["ref"]
        self.submit(ref)
        path = os.path.join(os.path.dirname(self.app.config["DATABASE"]), "upi_payments.csv")
        text = open(path, encoding="utf-8").read()
        for needle in (ref, STUDENT[1], STUDENT[0], "510.00", self.UTR, "Verifying", "Submitted (IST)"):
            self.assertIn(needle, text)
        self.decide(ref, "approve")
        text = open(path, encoding="utf-8").read()
        self.assertIn("Approved", text)
        a = self.admin_client()
        r = a.get("/api/admin/upi/export.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["Content-Type"])
        self.assertIn(self.UTR, r.get_data(as_text=True))
        self.assertTrue(r.get_data(as_text=True).startswith("\ufeff"))

    def test_names_that_look_like_excel_formulas_are_neutralised(self):
        conn = connect(self.app.config["DATABASE"])
        uid = create_user(conn, "evil@gmail.com", "=HYPERLINK(\"http://x\")", "Maple-River-Lamp-47!", "student"); conn.close()
        c = self.app.test_client()
        self.login("evil@gmail.com", "Maple-River-Lamp-47!", client=c)
        self.submit(self.order(client=c)["ref"], client=c)
        text = self.admin_client().get("/api/admin/upi/export.csv").get_data(as_text=True)
        self.assertIn("'=HYPERLINK", text)
        self.assertNotIn(",=HYPERLINK", text)

    def test_receipt_only_for_the_payer_and_only_once_approved(self):
        self.student()
        ref = self.order()["ref"]
        self.assertEqual(self.client.get(f"/upi/receipts/{ref}").status_code, 404)
        self.submit(ref)
        self.assertEqual(self.client.get(f"/upi/receipts/{ref}").status_code, 404)
        self.decide(ref, "approve")
        r = self.client.get(f"/upi/receipts/{ref}")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        for needle in (ref, STUDENT[1], "510.00", self.UTR, "codeskysindia@okhdfcbank", "not a GST tax invoice"):
            self.assertIn(needle, page)
        self.assertNotIn(STUDENT[0], page)               # email addresses are not printed on the receipt
        conn = connect(self.app.config["DATABASE"])
        create_user(conn, "eve@gmail.com", "Eve", "Maple-River-Lamp-47!", "student"); conn.close()
        eve = self.app.test_client()
        self.login("eve@gmail.com", "Maple-River-Lamp-47!", client=eve)
        self.assertEqual(eve.get(f"/upi/receipts/{ref}").status_code, 404)

    def test_the_admin_is_told_when_a_payment_needs_checking(self):
        self.student()
        self.submit(self.order()["ref"])
        conn = connect(self.app.config["DATABASE"])
        n = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND title LIKE 'UPI payment%'",
                         (self.admin_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)


class UpiPrivacyAndEmailTests(UpiBase):
    def test_email_addresses_are_only_shown_to_administrators(self):
        self.student()
        ref = self.order()["ref"]
        self.assertNotIn("user_email", self.client.get(f"/api/upi/payments/{ref}").get_json()["payment"])
        self.assertNotIn(STUDENT[0], self.client.get("/api/upi").get_data(as_text=True))
        self.submit(ref)
        row = self.admin_client().get("/api/admin/upi").get_json()["payments"][0]
        self.assertEqual(row["user_email"], STUDENT[0])

    def test_admins_are_emailed_on_submit_and_the_payer_on_approval(self):
        self.student()
        ref = self.order()["ref"]
        self.submit(ref)
        box = self.app.config["MAIL_OUTBOX"]
        self.assertTrue(any(m["to"] == ADMIN[0] and "UPI payment to verify" in m["subject"] and "8 hours" in m["text"] for m in box))
        self.decide(ref, "approve")
        self.assertTrue(any(m["to"] == STUDENT[0] and "upgraded" in m["subject"] for m in box))
        self.decide(self.order("hosting-lifetime")["ref"], "approve")      # not submitted: refused, no mail
        self.assertEqual(sum(1 for m in box if m["to"] == STUDENT[0]), 1)

    def test_a_mail_failure_never_breaks_a_payment(self):
        from unittest import mock
        self.student()
        ref = self.order()["ref"]
        with mock.patch("kova.mailer.send_mail", side_effect=RuntimeError("smtp down")):
            self.assertEqual(self.submit(ref).status_code, 200)
            self.assertEqual(self.decide(ref, "approve").status_code, 200)
