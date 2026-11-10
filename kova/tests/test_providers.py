"""Git provider OAuth, repository access and push webhooks."""
import hashlib
import hmac
import json
from unittest import mock

from kova.crypto import Secrets, normalise_key
from kova.db import connect

from .base import ADMIN, STUDENT, AppTestCase
from .test_platform import PlatformCase


def gh_sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"status {self.status_code}")


class CryptoTests(AppTestCase):
    def test_round_trip_and_tamper_detection(self):
        box = Secrets(normalise_key("a-passphrase-for-tests"))
        token = "gho_" + "x" * 36
        cipher = box.encrypt(token)
        self.assertNotIn(token, cipher)
        self.assertEqual(box.decrypt(cipher), token)
        from kova.crypto import EncryptionError
        with self.assertRaises(EncryptionError):
            Secrets(normalise_key("a-different-passphrase")).decrypt(cipher)


class ProviderConnectionTests(PlatformCase):
    def test_providers_list_reports_configuration(self):
        self.as_student()
        data = self.client.get("/api/git/providers").get_json()["providers"]
        names = {p["provider"]: p for p in data}
        self.assertFalse(names["github"]["configured"], "no client id configured in tests")
        self.assertFalse(names["github"]["connected"])
        self.assertIn("/api/git/github/callback", names["github"]["callback_url"])

    def test_authorize_refuses_when_oauth_is_not_configured(self):
        self.as_student()
        r = self.client.get("/api/git/github/authorize")
        self.assertEqual(r.status_code, 503)
        self.assertIn("GITHUB_CLIENT_ID", r.get_json()["error"])

    def test_authorize_returns_provider_url_with_state(self):
        self.as_student()
        self.app.config["GITHUB_CLIENT_ID"] = "cid"
        self.app.config["GITHUB_CLIENT_SECRET"] = "csecret"
        url = self.client.get("/api/git/github/authorize").get_json()["url"]
        self.assertTrue(url.startswith("https://github.com/login/oauth/authorize?"))
        self.assertIn("client_id=cid", url)
        self.assertIn("state=", url)
        self.assertNotIn("csecret", url, "the client secret must never leave the backend")

    def test_callback_rejects_a_forged_state(self):
        self.as_student()
        self.app.config["GITHUB_CLIENT_ID"] = "cid"
        self.app.config["GITHUB_CLIENT_SECRET"] = "csecret"
        self.client.get("/api/git/github/authorize")
        r = self.client.get("/api/git/github/callback?code=abc&state=not-the-nonce")
        self.assertEqual(r.status_code, 400)
        self.assertIn("could not be verified", r.get_json()["error"])

    def test_callback_stores_an_encrypted_token(self):
        self.as_student()
        self.app.config["GITHUB_CLIENT_ID"] = "cid"
        self.app.config["GITHUB_CLIENT_SECRET"] = "csecret"
        url = self.client.get("/api/git/github/authorize").get_json()["url"]
        state = url.split("state=")[1].split("&")[0]
        secret_token = "gho_secrettokenvalue1234567890"
        with mock.patch("kova.providers.requests.post",
                        return_value=FakeResponse({"access_token": secret_token, "scope": "repo"})), \
             mock.patch("kova.providers.requests.get",
                        return_value=FakeResponse({"id": 7, "login": "student1", "name": "Student One"})):
            r = self.client.get(f"/api/git/github/callback?code=abc&state={state}")
        self.assertEqual(r.status_code, 302)

        conn = connect(self.app.config["DATABASE"])
        row = conn.execute("SELECT * FROM git_accounts WHERE provider='github'").fetchone()
        conn.close()
        self.assertEqual(row["username"], "student1")
        self.assertNotIn(secret_token, row["token_cipher"], "token must be encrypted at rest")

        # And it must not come back out through any API the browser can call.
        listed = self.client.get("/api/git/providers").get_data(as_text=True)
        self.assertNotIn(secret_token, listed)
        self.assertIn("student1", listed)

    def test_repos_requires_a_connected_account(self):
        self.as_student()
        r = self.client.get("/api/git/github/repos")
        self.assertEqual(r.status_code, 409)

    def test_repos_and_branches_from_the_provider(self):
        self.as_student()
        self._connect_github()
        payload = [{"id": 1, "full_name": "student1/site", "name": "site", "html_url": "https://github.com/student1/site",
                    "private": True, "default_branch": "main", "description": "d", "language": "JS",
                    "pushed_at": "2026-10-01T00:00:00Z"}]
        with mock.patch("kova.providers.requests.get", return_value=FakeResponse(payload)):
            repos = self.client.get("/api/git/github/repos").get_json()["repos"]
        self.assertEqual(repos[0]["full_name"], "student1/site")
        self.assertTrue(repos[0]["private"])
        with mock.patch("kova.providers.requests.get",
                        return_value=FakeResponse([{"name": "main"}, {"name": "dev"}])):
            branches = self.client.get("/api/git/github/branches?repo=student1/site").get_json()["branches"]
        self.assertEqual(branches, ["main", "dev"])

    def test_disconnect(self):
        self.as_student()
        self._connect_github()
        r = self.client.call("delete", "/api/git/github") if hasattr(self.client, "call") else \
            self.call("delete", "/api/git/github")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/git/github/repos").status_code, 409)

    def test_private_repo_needs_a_connected_account(self):
        self.as_student()
        r = self.create(is_private=True, deploy=False)
        self.assertEqual(r.status_code, 409)
        self.assertIn("Connect your Github account", r.get_json()["error"])

    def _connect_github(self):
        self.app.config["GITHUB_CLIENT_ID"] = "cid"
        self.app.config["GITHUB_CLIENT_SECRET"] = "csecret"
        url = self.client.get("/api/git/github/authorize").get_json()["url"]
        state = url.split("state=")[1].split("&")[0]
        with mock.patch("kova.providers.requests.post",
                        return_value=FakeResponse({"access_token": "gho_tok", "scope": "repo"})), \
             mock.patch("kova.providers.requests.get",
                        return_value=FakeResponse({"id": 7, "login": "student1", "name": "Student One"})):
            self.client.get(f"/api/git/github/callback?code=abc&state={state}")


class GitHubWebhookTests(PlatformCase):
    def setUp(self):
        super().setUp()
        self.as_student()
        self.project = self.create(deploy=False).get_json()["project"]
        conn = connect(self.app.config["DATABASE"])
        row = conn.execute("SELECT hook_token, webhook_secret FROM projects WHERE slug='web-app'").fetchone()
        conn.close()
        self.hook_id = row["hook_token"]
        self.secret = row["webhook_secret"]
        self.anon = self.app.test_client()

    def push(self, branch="main", sha="a" * 40, delivery="d1", secret=None, event="push"):
        body = json.dumps({"ref": f"refs/heads/{branch}", "after": sha,
                           "head_commit": {"id": sha, "message": "Fix the thing"}}).encode()
        return self.anon.post(f"/api/hooks/github/{self.hook_id}", data=body, headers={
            "Content-Type": "application/json", "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": gh_sig(secret or self.secret, body)})

    def test_valid_signature_queues_a_deployment(self):
        r = self.push()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        dep_id = r.get_json()["deployment_id"]
        d = self.wait(dep_id)
        self.assertEqual(d["status"], "ready")
        self.assertEqual(d["trigger"], "github-push")
        self.assertEqual(d["commit_message"], "Fix the thing")

    def test_bad_signature_is_rejected(self):
        r = self.push(secret="the-wrong-secret")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Signature verification failed", r.get_json()["message"])

    def test_missing_signature_is_rejected(self):
        body = b'{"ref":"refs/heads/main"}'
        r = self.anon.post(f"/api/hooks/github/{self.hook_id}", data=body,
                           headers={"Content-Type": "application/json", "X-GitHub-Event": "push"})
        self.assertEqual(r.status_code, 401)

    def test_unknown_hook_id_looks_identical_to_a_bad_signature(self):
        body = b"{}"
        r = self.anon.post("/api/hooks/github/" + "z" * 30, data=body, headers={
            "Content-Type": "application/json", "X-GitHub-Event": "push",
            "X-Hub-Signature-256": gh_sig("whatever", body)})
        self.assertEqual(r.status_code, 401)
        self.assertIn("Signature verification failed", r.get_json()["message"])

    def test_duplicate_delivery_is_ignored(self):
        first = self.push(delivery="same-id")
        self.assertIn("deployment_id", first.get_json())
        second = self.push(delivery="same-id")
        self.assertNotIn("deployment_id", second.get_json())
        self.assertIn("Duplicate", second.get_json()["message"])

    def test_push_to_another_branch_is_ignored(self):
        r = self.push(branch="feature/x", delivery="d2")
        self.assertNotIn("deployment_id", r.get_json())
        self.assertIn("not this project's deploy branch", r.get_json()["message"])

    def test_same_commit_is_not_built_twice_concurrently(self):
        self.push(delivery="d1")
        r = self.push(delivery="d2")   # different delivery, same commit, still building
        self.assertNotIn("deployment_id", r.get_json())
        self.assertIn("already building", r.get_json()["message"])

    def test_ping_is_acknowledged_without_deploying(self):
        r = self.push(event="ping", delivery="p1")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("deployment_id", r.get_json())

    def test_auto_deploy_off_blocks_the_webhook(self):
        self.call("patch", "/api/projects/web-app", {"auto_deploy": False})
        r = self.push(delivery="d3")
        self.assertNotIn("deployment_id", r.get_json())
        self.assertIn("turned off", r.get_json()["message"])

    def test_branch_deletion_is_ignored(self):
        body = json.dumps({"ref": "refs/heads/main", "deleted": True, "after": "0" * 40}).encode()
        r = self.anon.post(f"/api/hooks/github/{self.hook_id}", data=body, headers={
            "Content-Type": "application/json", "X-GitHub-Event": "push", "X-GitHub-Delivery": "del1",
            "X-Hub-Signature-256": gh_sig(self.secret, body)})
        self.assertNotIn("deployment_id", r.get_json())

    def test_webhook_secret_is_never_returned_to_the_browser(self):
        body = self.client.get("/api/projects/web-app").get_data(as_text=True)
        self.assertNotIn(self.secret, body)
        self.assertIn("secret_set", body)


class GitLabWebhookTests(PlatformCase):
    def setUp(self):
        super().setUp()
        self.as_student()
        self.create(deploy=False)
        conn = connect(self.app.config["DATABASE"])
        row = conn.execute("SELECT hook_token, webhook_secret FROM projects WHERE slug='web-app'").fetchone()
        conn.close()
        self.hook_id, self.secret = row["hook_token"], row["webhook_secret"]
        self.anon = self.app.test_client()

    def push(self, token=None, delivery="g1", branch="main"):
        body = json.dumps({"ref": f"refs/heads/{branch}", "after": "b" * 40,
                           "commits": [{"message": "GitLab commit"}]}).encode()
        return self.anon.post(f"/api/hooks/gitlab/{self.hook_id}", data=body, headers={
            "Content-Type": "application/json", "X-Gitlab-Event": "Push Hook",
            "X-Gitlab-Event-UUID": delivery, "X-Gitlab-Token": token or self.secret})

    def test_valid_token_queues_a_deployment(self):
        r = self.push()
        self.assertEqual(r.status_code, 200)
        d = self.wait(r.get_json()["deployment_id"])
        self.assertEqual(d["trigger"], "gitlab-push")
        self.assertEqual(d["commit_message"], "GitLab commit")

    def test_wrong_token_is_rejected(self):
        r = self.push(token="nope")
        self.assertEqual(r.status_code, 401)

    def test_duplicate_delivery_ignored(self):
        self.push(delivery="same")
        r = self.push(delivery="same")
        self.assertIn("Duplicate", r.get_json()["message"])
