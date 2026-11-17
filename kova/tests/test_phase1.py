import unittest

from kova.security import hash_password, verify_password
from kova.users import ValidationError, create_user
from kova.db import connect

from .base import ADMIN, STUDENT, AppTestCase


class SecurityTests(unittest.TestCase):
    def test_hash_roundtrip(self):
        h = hash_password("a-long-password-1")
        self.assertTrue(verify_password("a-long-password-1", h))
        self.assertFalse(verify_password("wrong", h))
        self.assertNotEqual(h, hash_password("a-long-password-1"))  # unique salt

    def test_tampered_or_malformed_hash_rejected(self):
        self.assertFalse(verify_password("x", "garbage"))
        self.assertFalse(verify_password("x", "md5$1$2$3$aa$bb"))


class HealthAndPages(AppTestCase):
    def test_health_ok_without_auth(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["database"])

    def test_login_page_is_branded(self):
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Codeskys", r.data)
        self.assertNotIn(b"KOVA", r.data)

    def test_home_shows_the_public_landing_page_when_signed_out(self):
        # Since the Codeskys rebrand "/" is the public landing page, not a redirect.
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Codeskys", r.data)
        self.assertIn(b"/login", r.data)

    def test_security_headers(self):
        r = self.client.get("/login")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
        self.assertNotIn("<script>", r.get_data(as_text=True).replace("<script src", ""))  # no inline scripts

    def test_static_assets_served(self):
        for p in ("css/tokens.css", "css/app.css", "css/fonts.css", "css/site.css",
                  "js/api.js", "js/login.js", "js/icons.js", "js/app.js",
                  "fonts/inter-latin-400-normal.woff2"):
            r = self.client.get("/static/" + p)
            r.close()
            self.assertEqual(r.status_code, 200, p)


class AuthTests(AppTestCase):
    def test_me_anonymous_gives_csrf_and_no_user(self):
        data = self.client.get("/api/auth/me").get_json()
        self.assertIsNone(data["user"])
        self.assertTrue(data["csrf"])

    def test_post_without_csrf_rejected(self):
        r = self.client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[2]})
        self.assertEqual(r.status_code, 403)

    def test_post_with_wrong_csrf_rejected(self):
        self.csrf()
        r = self.post("/api/auth/login", {"email": ADMIN[0], "password": ADMIN[2]}, token="nope")
        self.assertEqual(r.status_code, 403)

    def test_login_success_and_session(self):
        r = self.login(ADMIN[0], ADMIN[2])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["user"]["role"], "admin")
        self.assertNotIn("password_hash", r.get_data(as_text=True))
        me = self.client.get("/api/auth/me").get_json()
        self.assertEqual(me["user"]["email"], ADMIN[0])

    NEW = {"name": "New Student", "email": "new.student@gmail.com", "password": "Maple-River-Lamp-47!"}

    def _code(self):
        """The 6-digit code from the most recent email 'sent' by the app."""
        import re
        body = self.app.config["MAIL_OUTBOX"][-1]["text"]
        return re.search(r"\b(\d{6})\b", body).group(1)

    def test_signup_needs_an_email_code_before_the_account_exists(self):
        r = self.post("/api/auth/register", self.NEW)
        self.assertEqual(r.status_code, 202, r.get_json())
        self.assertTrue(r.get_json()["verify"])
        # Nothing is created or signed in yet, and the code is never in the response.
        self.assertIsNone(self.client.get("/api/auth/me").get_json()["user"])
        self.assertNotIn(self._code(), r.get_data(as_text=True))
        conn = connect(self.app.config["DATABASE"])
        self.assertIsNone(conn.execute("SELECT 1 FROM users WHERE email = ?", (self.NEW["email"],)).fetchone())
        row = conn.execute("SELECT * FROM pending_signups").fetchone()
        self.assertNotIn(self._code(), row["code_hash"])   # only a hash is stored
        conn.close()
        ok = self.post("/api/auth/register/verify", {"email": self.NEW["email"], "code": self._code()})
        self.assertEqual(ok.status_code, 201, ok.get_json())
        self.assertEqual(ok.get_json()["user"]["role"], "student")
        self.assertEqual(self.client.get("/api/auth/me").get_json()["user"]["email"], self.NEW["email"])
        self.assertNotIn("password_hash", ok.get_data(as_text=True))
        # And the password chosen in step 1 is the one that works.
        other = self.app.test_client()
        self.assertEqual(self.login(self.NEW["email"], self.NEW["password"], client=other).status_code, 200)

    def test_wrong_code_is_refused_and_locks_after_five_tries(self):
        self.post("/api/auth/register", self.NEW)
        right = self._code()
        wrong = "000000" if right != "000000" else "111111"
        for _ in range(4):
            self.assertEqual(self.post("/api/auth/register/verify", {"email": self.NEW["email"], "code": wrong}).status_code, 400)
        locked = self.post("/api/auth/register/verify", {"email": self.NEW["email"], "code": wrong})
        self.assertEqual(locked.status_code, 429)
        # Even the right code no longer works: the sign-up must be started again.
        self.assertEqual(self.post("/api/auth/register/verify", {"email": self.NEW["email"], "code": right}).status_code, 404)

    def test_expired_code_is_refused(self):
        self.post("/api/auth/register", self.NEW)
        conn = connect(self.app.config["DATABASE"])
        conn.execute("UPDATE pending_signups SET expires_at = '2000-01-01T00:00:00+00:00'")
        conn.commit(); conn.close()
        r = self.post("/api/auth/register/verify", {"email": self.NEW["email"], "code": self._code()})
        self.assertEqual(r.status_code, 410)

    def test_resend_is_throttled_then_issues_a_new_code(self):
        self.post("/api/auth/register", self.NEW)
        first = self._code()
        self.assertEqual(self.post("/api/auth/register/resend", {"email": self.NEW["email"]}).status_code, 429)
        conn = connect(self.app.config["DATABASE"])
        conn.execute("UPDATE pending_signups SET last_sent_at = '2000-01-01T00:00:00+00:00'")
        conn.commit(); conn.close()
        self.assertEqual(self.post("/api/auth/register/resend", {"email": self.NEW["email"]}).status_code, 200)
        self.assertEqual(len(self.app.config["MAIL_OUTBOX"]), 2)
        if self._code() != first:   # the first code is dead once a new one is issued
            self.assertEqual(self.post("/api/auth/register/verify", {"email": self.NEW["email"], "code": first}).status_code, 400)

    def test_public_registration_cannot_create_admin(self):
        r = self.post("/api/auth/register", {**self.NEW, "role": "admin"})
        self.assertEqual(r.status_code, 202)
        ok = self.post("/api/auth/register/verify", {"email": self.NEW["email"], "code": self._code()})
        self.assertEqual(ok.get_json()["user"]["role"], "student")

    def test_fake_and_disposable_emails_are_blocked(self):
        for bad in ("test@gmail.com", "someone@mailinator.com", "me@example.com", "aaaa@gmail.com",
                    "no-at-sign", "a@b", "x@localhost", "name@domain.test"):
            r = self.post("/api/auth/register", {**self.NEW, "email": bad})
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.app.config.get("MAIL_OUTBOX", []), [])   # nothing was emailed

    def test_weak_passwords_are_blocked(self):
        for weak in ("short1A!", "alllowercase123!", "ALLUPPERCASE123!", "NoNumbersHere!!", "NoSymbols12345Ab",
                     "Password@12345", "aaaaaaaaaaaA1!"):
            r = self.post("/api/auth/register", {**self.NEW, "password": weak})
            self.assertEqual(r.status_code, 400, weak)
        self.assertEqual(self.app.config.get("MAIL_OUTBOX", []), [])

    def test_existing_accounts_can_still_log_in_with_older_weaker_passwords(self):
        # Rules apply when a password is CREATED. Logging in never re-checks the rules.
        self.assertEqual(self.login(STUDENT[0], STUDENT[2]).status_code, 200)

    def test_duplicate_email_is_refused(self):
        conn = connect(self.app.config["DATABASE"])
        create_user(conn, "taken@gmail.com", "Taken", "Maple-River-Lamp-47!", "student")
        conn.close()
        r = self.post("/api/auth/register", {**self.NEW, "email": "taken@gmail.com"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.app.config.get("MAIL_OUTBOX", []), [])

    def test_otp_can_be_switched_off(self):
        self.app.config["EMAIL_OTP_REQUIRED"] = False
        r = self.post("/api/auth/register", self.NEW)
        self.assertEqual(r.status_code, 201)

    def test_registration_requires_csrf(self):
        r = self.client.post("/api/auth/register", json={"name": "New", "email": "new@gmail.com", "password": "Maple-River-Lamp-47!"})
        self.assertEqual(r.status_code, 403)

    def test_email_is_case_insensitive(self):
        self.assertEqual(self.login("ADMIN@Example.test", ADMIN[2]).status_code, 200)

    def test_wrong_password_and_unknown_email_look_identical(self):
        a = self.login(ADMIN[0], "wrong-password-xx")
        b = self.login("nobody@example.test", "wrong-password-xx")
        self.assertEqual(a.status_code, 401)
        self.assertEqual(b.status_code, 401)
        self.assertEqual(a.get_json(), b.get_json())

    def test_lockout_after_repeated_failures(self):
        for _ in range(5):
            self.assertEqual(self.login(STUDENT[0], "bad-password-xx").status_code, 401)
        r = self.login(STUDENT[0], STUDENT[2])  # even the right password is blocked while locked
        self.assertEqual(r.status_code, 429)

    def test_inactive_user_cannot_login(self):
        conn = connect(self.app.config["DATABASE"])
        conn.execute("UPDATE users SET is_active=0 WHERE id=?", (self.student_id,))
        conn.commit(); conn.close()
        self.assertEqual(self.login(STUDENT[0], STUDENT[2]).status_code, 401)

    def test_logout_clears_session(self):
        self.login(ADMIN[0], ADMIN[2])
        self.assertEqual(self.post("/api/auth/logout").status_code, 200)
        self.assertIsNone(self.client.get("/api/auth/me").get_json()["user"])

    def test_session_fixation_csrf_rotates_on_login(self):
        before = self.csrf()
        after = self.login(ADMIN[0], ADMIN[2]).get_json()["csrf"]
        self.assertNotEqual(before, after)

    def test_login_events_are_audited(self):
        self.login(ADMIN[0], "wrong-password-xx")
        self.login(ADMIN[0], ADMIN[2])
        conn = connect(self.app.config["DATABASE"])
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
        conn.close()
        self.assertEqual(actions, ["login.failed", "login.ok"])


class StatusTests(AppTestCase):
    def test_status_requires_login(self):
        self.assertEqual(self.client.get("/api/platform/status").status_code, 401)

    def test_status_reports_coolify_not_configured(self):
        self.login(STUDENT[0], STUDENT[2])
        data = self.client.get("/api/platform/status").get_json()
        self.assertEqual(data["engine"]["state"], "not_configured")
        self.assertNotIn("coolify", data)  # backend details are for administrators
        self.assertIsNone(data["users"])  # students don't see user counts

    def test_status_admin_sees_user_count_and_never_the_token(self):
        self.app.config.update(COOLIFY_URL="https://coolify.example.test", COOLIFY_TOKEN="super-secret-token")
        self.login(ADMIN[0], ADMIN[2])
        r = self.client.get("/api/platform/status")
        self.assertEqual(r.get_json()["users"], 2)
        self.assertEqual(r.get_json()["coolify"]["host"], "coolify.example.test")
        self.assertNotIn("super-secret-token", r.get_data(as_text=True))

    def test_authenticated_home_renders_shell(self):
        self.login(ADMIN[0], ADMIN[2])
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'id="shell"', r.data)      # app shell mounts client-side
        self.assertIn(b'id="sidebar"', r.data)
        self.assertIn(b"js/app.js", r.data)


class UserCreationTests(AppTestCase):
    def test_validation(self):
        conn = connect(self.app.config["DATABASE"])
        for args in [("bad", "N", "long-enough-pass1"), ("a@b.co", "", "long-enough-pass1"),
                     ("a@b.co", "N", "short"), (STUDENT[0], "Dup", "long-enough-pass1")]:
            with self.assertRaises(ValidationError):
                create_user(conn, *args)
        conn.close()


if __name__ == "__main__":
    unittest.main()
