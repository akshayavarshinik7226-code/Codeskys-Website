"""Connecting GitHub through the Codeskys GitHub App (user-to-server tokens).

The App's Client ID/secret are used for the same OAuth flow as an OAuth App, but the token
expires after 8 hours and comes with a refresh token. Without refreshing, every student would
have to reconnect GitHub three times a day."""
import time
from unittest import mock

from kova.db import connect

from .test_coolify import CoolifyCase
from .test_platform import PlatformCase
from .test_providers import FakeResponse

ACCESS1, REFRESH1 = "ghu_" + "a" * 36, "ghr_" + "b" * 76
ACCESS2, REFRESH2 = "ghu_" + "c" * 36, "ghr_" + "d" * 76


class GitHubAppLoginTests(PlatformCase):
    def setUp(self):
        super().setUp()
        self.as_student()
        self.app.config.update(GITHUB_CLIENT_ID="Iv23liTEST", GITHUB_CLIENT_SECRET="test-client-secret")

    def connect(self, token_body):
        url = self.client.get("/api/git/github/authorize").get_json()["url"]
        state = url.split("state=")[1].split("&")[0]
        with mock.patch("kova.providers.requests.post", return_value=FakeResponse(token_body)), \
             mock.patch("kova.providers.requests.get",
                        return_value=FakeResponse({"id": 7, "login": "student1", "name": "S"})):
            self.assertEqual(self.client.get(f"/api/git/github/callback?code=c&state={state}").status_code, 302)
        return self.account()

    def account(self):
        conn = connect(self.app.config["DATABASE"])
        self.addCleanup(conn.close)
        return conn.execute("SELECT * FROM git_accounts WHERE provider = 'github'").fetchone()

    def repos(self, post=None):
        seen = {}

        def get(url, **kw):
            seen["auth"] = kw["headers"]["Authorization"]
            return FakeResponse([])
        with mock.patch("kova.providers.requests.get", side_effect=get), \
             mock.patch("kova.providers.requests.post", side_effect=post or AssertionError("no refresh expected")):
            r = self.client.get("/api/git/github/repos")
        return r, seen.get("auth")

    def test_expiring_token_and_refresh_token_are_stored_encrypted(self):
        row = self.connect({"access_token": ACCESS1, "expires_in": 28800, "refresh_token": REFRESH1,
                            "refresh_token_expires_in": 15897600, "scope": ""})
        self.assertTrue(row["token_expires_at"])
        self.assertTrue(row["refresh_cipher"])
        for raw in (ACCESS1, REFRESH1):
            self.assertNotIn(raw, row["token_cipher"] + row["refresh_cipher"])
        listed = self.client.get("/api/git/providers").get_data(as_text=True)
        self.assertNotIn("ghu_", listed)
        self.assertNotIn("ghr_", listed)

    def test_a_valid_token_is_used_without_refreshing(self):
        self.connect({"access_token": ACCESS1, "expires_in": 28800, "refresh_token": REFRESH1})
        r, auth = self.repos()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(auth, f"Bearer {ACCESS1}")

    def test_an_expired_token_is_refreshed_and_the_new_refresh_token_kept(self):
        self.connect({"access_token": ACCESS1, "expires_in": 28800, "refresh_token": REFRESH1})
        conn = connect(self.app.config["DATABASE"])
        conn.execute("UPDATE git_accounts SET token_expires_at = '2020-01-01T00:00:00+00:00'")
        conn.commit(); conn.close()
        sent = {}

        def post(url, **kw):
            sent.update(kw["data"], url=url)
            return FakeResponse({"access_token": ACCESS2, "expires_in": 28800, "refresh_token": REFRESH2})
        r, auth = self.repos(post)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(auth, f"Bearer {ACCESS2}")
        self.assertEqual((sent["grant_type"], sent["refresh_token"], sent["client_id"]),
                         ("refresh_token", REFRESH1, "Iv23liTEST"))
        self.assertEqual(sent["url"], "https://github.com/login/oauth/access_token")
        row = self.account()
        self.assertGreater(row["token_expires_at"], time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
        # The rotated refresh token replaced the old one (GitHub's are single-use).
        r, auth = self.repos()
        self.assertEqual(auth, f"Bearer {ACCESS2}")

    def test_a_refused_refresh_asks_the_student_to_reconnect(self):
        self.connect({"access_token": ACCESS1, "expires_in": 28800, "refresh_token": REFRESH1})
        conn = connect(self.app.config["DATABASE"])
        conn.execute("UPDATE git_accounts SET token_expires_at = '2020-01-01T00:00:00+00:00'")
        conn.commit(); conn.close()
        r, _ = self.repos(lambda url, **kw: FakeResponse({"error": "bad_refresh_token",
                                                          "error_description": "The refresh token passed is incorrect"}))
        self.assertEqual(r.status_code, 409)
        self.assertIn("Reconnect", r.get_json()["error"])

    def test_classic_oauth_app_tokens_still_work(self):
        row = self.connect({"access_token": "gho_" + "e" * 36, "scope": "read:user"})
        self.assertFalse(row["token_expires_at"])
        r, auth = self.repos()
        self.assertEqual(r.status_code, 200)


class PrivateRepoTests(CoolifyCase):
    def test_private_repository_is_refused_when_the_project_is_created(self):
        self.configure_coolify()
        self.as_student()
        from kova.db import connect as c
        conn = c(self.app.config["DATABASE"])
        conn.execute("INSERT INTO git_accounts (user_id, provider, external_id, username, display_name, host, scopes, "
                     "token_cipher, created_at, updated_at) VALUES (?, 'github', '7', 's', 's', 'github.com', '', 'x', "
                     "'2026', '2026')", (self.student_id,))
        conn.commit(); conn.close()
        r = self.create(is_private=True, deploy=False)
        self.assertEqual(r.status_code, 422)
        self.assertIn("Private repositories cannot be deployed yet", r.get_json()["error"])
