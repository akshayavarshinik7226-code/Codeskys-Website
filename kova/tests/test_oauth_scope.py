"""GitHub OAuth asks for least privilege by default (no write access to repositories)."""
from urllib.parse import parse_qs, urlparse

from .base import STUDENT, AppTestCase


class OAuthScopeTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(GITHUB_CLIENT_ID="test-client-id", GITHUB_CLIENT_SECRET="test-client-secret",
                               PUBLIC_URL="https://codeskys.com")
        self.login(STUDENT[0], STUDENT[2])

    def authorize_params(self):
        r = self.client.get("/api/git/github/authorize")
        self.assertEqual(r.status_code, 200)
        url = r.get_json()["url"]
        self.assertTrue(url.startswith("https://github.com/login/oauth/authorize?"))
        return parse_qs(urlparse(url).query)

    def test_default_scope_has_no_repo_write(self):
        q = self.authorize_params()
        self.assertEqual(q["scope"], ["read:user"])
        self.assertEqual(q["redirect_uri"], ["https://codeskys.com/api/git/github/callback"])
        self.assertTrue(q["state"][0])
        self.assertNotIn("test-client-secret", self.client.get("/api/git/providers").get_data(as_text=True))

    def test_owner_can_opt_in_explicitly(self):
        self.app.config["GITHUB_OAUTH_SCOPE"] = "repo read:user"
        self.assertEqual(self.authorize_params()["scope"], ["repo read:user"])
