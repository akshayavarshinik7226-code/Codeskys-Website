"""One account per browser, admin exemptions, long sessions, and per-visitor rate limits behind a proxy."""
import re

from kova.db import connect
from kova.users import create_user
from .base import ADMIN, STUDENT, AppTestCase

PW = "Maple-River-Lamp-47!"


class BrowserTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.app.config["ONE_ACCOUNT_PER_BROWSER"] = True

    def signup(self, client, email, name="New Person"):
        r = self.post("/api/auth/register", {"name": name, "email": email, "password": PW}, client=client)
        if r.status_code != 202:
            return r
        code = re.search(r"\b(\d{6})\b", self.app.config["MAIL_OUTBOX"][-1]["text"]).group(1)
        return self.post("/api/auth/register/verify", {"email": email, "code": code}, client=client)

    def logout(self, client):
        client.post("/api/auth/logout", headers={"X-CSRF-Token": client.get("/api/auth/me").get_json()["csrf"]})

    def make_users(self, *emails):
        conn = connect(self.app.config["DATABASE"])
        for e in emails:
            create_user(conn, e, e.split("@")[0].title(), PW, "student")
        conn.close()

    # ---- one account per browser
    def test_a_second_account_cannot_be_created_in_the_same_browser(self):
        b = self.app.test_client()
        self.assertEqual(self.signup(b, "first.person@gmail.com").status_code, 201)
        self.logout(b)
        r = self.signup(b, "second.person@gmail.com")
        self.assertEqual(r.status_code, 403)
        self.assertIn("already has a Codeskys account", r.get_json()["error"])
        self.assertEqual(self.app.config["MAIL_OUTBOX"].__len__(), 1)        # no second code was even sent

    def test_a_linked_browser_cannot_log_in_as_someone_else(self):
        self.make_users("amy.one@gmail.com", "bob.two@gmail.com")
        b = self.app.test_client()
        self.assertEqual(self.login("amy.one@gmail.com", PW, client=b).status_code, 200)
        self.logout(b)
        r = self.login("bob.two@gmail.com", PW, client=b)
        self.assertEqual(r.status_code, 403)
        self.assertIn("a\u2022\u2022\u2022@gmail.com", r.get_json()["error"])   # the other account is masked
        self.assertEqual(b.get("/api/auth/me").get_json()["user"], None)
        self.assertEqual(self.login("amy.one@gmail.com", PW, client=b).status_code, 200)   # her own account is fine

    def test_another_browser_is_independent_and_one_person_can_use_many_browsers(self):
        self.make_users("amy.one@gmail.com", "bob.two@gmail.com")
        a, b = self.app.test_client(), self.app.test_client()
        self.assertEqual(self.login("amy.one@gmail.com", PW, client=a).status_code, 200)
        self.assertEqual(self.login("bob.two@gmail.com", PW, client=b).status_code, 200)
        c = self.app.test_client()
        self.assertEqual(self.login("amy.one@gmail.com", PW, client=c).status_code, 200)   # same person, 2nd device

    def test_the_browser_token_is_a_secure_cookie_and_only_its_hash_is_stored(self):
        self.make_users("amy.one@gmail.com")
        b = self.app.test_client()
        r = self.login("amy.one@gmail.com", PW, client=b)
        cookie = [c for c in r.headers.getlist("Set-Cookie") if c.startswith("kova_dev=")][0]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        token = cookie.split(";")[0].split("=", 1)[1]
        conn = connect(self.app.config["DATABASE"])
        stored = [r[0] for r in conn.execute("SELECT device_hash FROM browser_links")]
        conn.close()
        self.assertEqual(len(stored), 1)
        self.assertNotIn(token, stored)

    # ---- administrator browsers are unlimited
    def test_an_administrator_browser_can_create_and_use_many_accounts(self):
        self.make_users("amy.one@gmail.com", "bob.two@gmail.com")
        b = self.app.test_client()
        self.assertEqual(self.login(ADMIN[0], ADMIN[2], client=b).status_code, 200)
        self.logout(b)
        for email in ("amy.one@gmail.com", "bob.two@gmail.com"):
            self.assertEqual(self.login(email, PW, client=b).status_code, 200, email)
            self.logout(b)
        self.assertEqual(self.signup(b, "test.user.one@gmail.com").status_code, 201)
        self.logout(b)
        self.assertEqual(self.signup(b, "test.user.two@gmail.com").status_code, 201)

    def test_the_rule_can_be_switched_off(self):
        self.app.config["ONE_ACCOUNT_PER_BROWSER"] = False
        b = self.app.test_client()
        self.assertEqual(self.signup(b, "first.person@gmail.com").status_code, 201)
        self.logout(b)
        self.assertEqual(self.signup(b, "second.person@gmail.com").status_code, 201)


class AdminSignInTests(AppTestCase):
    def test_an_administrator_is_never_locked_out_but_a_student_is(self):
        for _ in range(8):
            self.assertEqual(self.login(ADMIN[0], "wrong-password-1", client=self.app.test_client()).status_code, 401)
        self.assertEqual(self.login(ADMIN[0], ADMIN[2]).status_code, 200)             # still gets in
        for _ in range(5):
            self.login(STUDENT[0], "wrong-password-1", client=self.app.test_client())
        self.assertEqual(self.login(STUDENT[0], STUDENT[2], client=self.app.test_client()).status_code, 429)   # locked

    def test_guessing_an_administrator_password_still_has_a_very_high_ceiling(self):
        self.app.config["ADMIN_LOGIN_FAILS_PER_HOUR"] = 5
        codes = [self.login(ADMIN[0], "wrong-password-1", client=self.app.test_client()).status_code for _ in range(8)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertIn(429, codes[5:])

    def test_the_administrator_is_not_throttled_by_other_peoples_attempts_from_the_same_address(self):
        for _ in range(25):
            self.login("nobody@gmail.com", "wrong-password-1", client=self.app.test_client())     # same address
        self.assertEqual(self.login("nobody@gmail.com", "wrong-password-1", client=self.app.test_client()).status_code, 429)
        self.assertEqual(self.login(ADMIN[0], ADMIN[2], client=self.app.test_client()).status_code, 200)

    def test_sign_in_lasts_beyond_the_browser_session(self):
        r = self.login(ADMIN[0], ADMIN[2], client=self.app.test_client())
        session_cookie = [c for c in r.headers.getlist("Set-Cookie") if c.startswith("session=")][0]
        self.assertRegex(session_cookie, r"Expires=")        # a persistent cookie, not "until the browser closes"


class ProxyAddressTests(AppTestCase):
    def test_each_visitor_behind_the_proxy_is_counted_by_their_own_address(self):
        conn = connect(self.app.config["DATABASE"]); conn.close()
        from kova import create_app
        import os
        app = create_app({"TESTING": True, "DATABASE": os.path.join(self.tmp, "p.db"), "SECRET_KEY": "x",
                          "TRUSTED_PROXIES": 1, "COOLIFY_URL": "", "COOLIFY_TOKEN": ""})
        c = app.test_client()
        csrf = c.get("/api/auth/me").get_json()["csrf"]
        def attempt(ip):
            return c.post("/api/auth/login", json={"email": "who@gmail.com", "password": "x" * 12},
                          headers={"X-CSRF-Token": csrf, "X-Forwarded-For": ip}).status_code
        for _ in range(21):
            attempt("203.0.113.5")
        self.assertEqual(attempt("203.0.113.5"), 429)          # that visitor is throttled...
        self.assertEqual(attempt("198.51.100.9"), 401)         # ...everyone else is not
