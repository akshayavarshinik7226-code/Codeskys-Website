"""Real Coolify API integration, driven against a mocked Coolify server.

These prove the request contract (method, path, body, teardown guards). They are
NOT evidence that a deployment works against a live Coolify server — that needs
an actual instance and is called out as untested in the README.
"""
import json
from unittest import mock

from kova import engine
from kova.db import connect, set_setting

from .base import ADMIN
from .test_platform import PlatformCase


class FakeCoolify:
    """Records calls and answers like a Coolify v1 server."""

    def __init__(self, deploy_status="finished", deploy_method_allowed="POST", app_name="web-app"):
        self.calls = []
        self.deploy_status = deploy_status
        self.deploy_method_allowed = deploy_method_allowed
        self.app_name = app_name

    def __call__(self, method, url, **kw):
        path = url.split("/api/v1", 1)[1].split("?")[0]
        self.calls.append({"method": method, "path": path, "json": kw.get("json"), "params": kw.get("params")})
        return _Resp(*self._route(method, path, kw))

    def _route(self, method, path, kw):
        if path == "/version":
            return {"version": "4.0.0"}, 200
        if path == "/servers":
            return [{"uuid": "srv-1", "name": "node-1", "ip": "10.0.0.1"}], 200
        if path == "/projects":
            return [{"uuid": "proj-1", "name": "students"}], 200
        if path == "/security/keys":
            return [{"uuid": "key-1", "name": "kova-deploy-key"}], 200
        if path in ("/applications/public", "/applications/private-deploy-key", "/applications/dockercompose"):
            return {"uuid": "app-123"}, 201
        if path == "/deploy":
            if method != self.deploy_method_allowed:
                return {"message": "Method Not Allowed"}, 405
            return {"deployments": [{"deployment_uuid": "dep-abc"}]}, 200
        if path.startswith("/deployments/") and path.endswith("/cancel"):
            return {"ok": True}, 200
        if path.startswith("/deployments/"):
            return {"status": self.deploy_status,
                    "logs": json.dumps([{"output": "npm install", "type": "stdout"},
                                        {"output": "built ok", "type": "stdout"},
                                        {"output": "hidden", "hidden": True}])}, 200
        if path.startswith("/applications/") and path.endswith("/envs/bulk"):
            return {"ok": True}, 200
        if path.startswith("/applications/") and path.endswith("/stop"):
            return {"ok": True}, 200
        if path.startswith("/applications/"):
            return {"uuid": "app-123", "name": self.app_name, "fqdn": "https://web-app.apps.example.com"}, 200
        return {"message": "not found"}, 404


class _Resp:
    def __init__(self, payload, status):
        self._p, self.status_code = payload, status
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class CoolifyCase(PlatformCase):
    def configure_coolify(self, **extra):
        conn = connect(self.app.config["DATABASE"])
        for k, v in {"coolify_url": "https://coolify.example.com", "coolify_token": "secret-coolify-token",
                     "coolify_project_uuid": "proj-1", "coolify_server_uuid": "srv-1",
                     "coolify_base_domain": "apps.example.com", "deploy_provider": "coolify", **extra}.items():
            set_setting(conn, k, v)
        conn.close()


class CoolifyDeploymentTests(CoolifyCase):
    def test_full_lifecycle_against_a_mock_server(self):
        self.configure_coolify()
        self.as_student()
        fake = FakeCoolify()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            dep_id = self.create().get_json()["deployment_id"]
            d = self.wait(dep_id)
        self.assertEqual(d["status"], "ready")
        self.assertEqual(d["provider"], "coolify")
        self.assertFalse(d["is_simulated"], "a Coolify build must never be flagged simulated")
        self.assertEqual(d["url"], "https://web-app.apps.example.com")

        paths = [(c["method"], c["path"]) for c in fake.calls]
        self.assertIn(("POST", "/applications/public"), paths)
        self.assertIn(("POST", "/deploy"), paths)
        self.assertTrue(any(p.startswith("/deployments/") for _, p in paths))

        create = next(c for c in fake.calls if c["path"] == "/applications/public")["json"]
        self.assertEqual(create["project_uuid"], "proj-1")
        self.assertEqual(create["server_uuid"], "srv-1")
        self.assertEqual(create["limits_memory"], "512M")
        self.assertEqual(create["limits_cpus"], "0.5")
        self.assertFalse(create["is_auto_deploy_enabled"],
                         "Codeskys drives deploys; Coolify must not also trigger them")

        logs = self.client.get(f"/api/deployments/{dep_id}").get_json()["logs"]
        text = " ".join(l["line"] for l in logs)
        self.assertIn("npm install", text)
        self.assertNotIn("hidden", text)

    def test_deploy_falls_back_to_get_on_older_coolify(self):
        self.configure_coolify()
        self.as_student()
        fake = FakeCoolify(deploy_method_allowed="GET")
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            d = self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(d["status"], "ready")
        methods = [c["method"] for c in fake.calls if c["path"] == "/deploy"]
        self.assertEqual(methods, ["POST", "GET"])

    def test_failed_remote_build_is_reported_as_failed(self):
        self.configure_coolify()
        self.as_student()
        with mock.patch("kova.engine.requests.request", side_effect=FakeCoolify(deploy_status="failed")):
            d = self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(d["status"], "failed")
        self.assertIn("Coolify reported the build as failed", d["error"])

    def test_api_error_surfaces_and_never_reports_success(self):
        self.configure_coolify()
        self.as_student()

        def broken(method, url, **kw):
            return _Resp({"message": "Insufficient permissions"}, 403)

        with mock.patch("kova.engine.requests.request", side_effect=broken):
            d = self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(d["status"], "failed")
        self.assertIn("403", d["error"])

    def test_unreachable_coolify_fails_the_build_with_a_clear_message(self):
        self.configure_coolify()
        self.as_student()
        import requests as rq
        with mock.patch("kova.engine.requests.request", side_effect=rq.ConnectionError("boom")):
            d = self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(d["status"], "failed")
        self.assertIn("Could not reach Coolify", d["error"])

    def test_private_repo_without_a_deploy_key_fails_with_instructions(self):
        self.configure_coolify()
        self.as_student()
        self._connect_github()
        with mock.patch("kova.engine.requests.request", side_effect=FakeCoolify()):
            dep_id = self.create(is_private=True).get_json()["deployment_id"]
            d = self.wait(dep_id)
        self.assertEqual(d["status"], "failed")
        self.assertIn("deploy key", d["error"])

    def test_private_repo_with_a_deploy_key_uses_the_private_endpoint(self):
        self.configure_coolify(coolify_private_key_uuid="key-1")
        self.as_student()
        self._connect_github()
        fake = FakeCoolify()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            d = self.wait(self.create(is_private=True).get_json()["deployment_id"])
        self.assertEqual(d["status"], "ready")
        create = next(c for c in fake.calls if c["path"] == "/applications/private-deploy-key")
        self.assertEqual(create["json"]["private_key_uuid"], "key-1")
        self.assertTrue(create["json"]["git_repository"].startswith("git@github.com:"))

    def test_cancelling_calls_coolify_cancel(self):
        self.configure_coolify()
        self.as_student()
        fake = FakeCoolify(deploy_status="in_progress")
        conn = self.app_conn()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            dep_id = self.create().get_json()["deployment_id"]
            # Ask for cancellation before the worker polls, the way the API does.
            engine.request_cancel(conn, dep_id)
            self.work()
        d = self.client.get(f"/api/deployments/{dep_id}").get_json()["deployment"]
        self.assertEqual(d["status"], "cancelled")

    def _connect_github(self):
        from .test_providers import FakeResponse
        self.app.config["GITHUB_CLIENT_ID"] = "cid"
        self.app.config["GITHUB_CLIENT_SECRET"] = "csecret"
        url = self.client.get("/api/git/github/authorize").get_json()["url"]
        state = url.split("state=")[1].split("&")[0]
        with mock.patch("kova.providers.requests.post",
                        return_value=FakeResponse({"access_token": "gho_tok", "scope": "repo"})), \
             mock.patch("kova.providers.requests.get",
                        return_value=FakeResponse({"id": 7, "login": "student1", "name": "S"})):
            self.client.get(f"/api/git/github/callback?code=abc&state={state}")


class CoolifyTeardownTests(CoolifyCase):
    def _deploy(self, fake):
        self.as_student()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            d = self.wait(self.create().get_json()["deployment_id"])
        return d

    def test_expiry_stops_and_deletes_only_the_kova_application(self):
        self.configure_coolify()
        fake = FakeCoolify()
        d = self._deploy(fake)
        conn = self.app_conn()
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            engine.expire_deployment(conn, self.app.config, {"deployment_id": d["id"]}, lambda *a, **k: True)
        paths = [(c["method"], c["path"]) for c in fake.calls]
        self.assertIn(("POST", "/applications/app-123/stop"), paths)
        self.assertIn(("DELETE", "/applications/app-123"), paths)
        delete = next(c for c in fake.calls if c["method"] == "DELETE")
        self.assertEqual(delete["params"]["delete_configurations"], "true")
        row = conn.execute("SELECT status, teardown_note FROM deployments WHERE id = ?", (d["id"],)).fetchone()
        self.assertEqual(row["status"], "expired")
        self.assertIn("stopped and deleted", row["teardown_note"])
        self.assertIsNone(conn.execute("SELECT provider_ref FROM projects WHERE slug='web-app'").fetchone()[0])

    def test_refuses_to_delete_an_application_it_did_not_create(self):
        self.configure_coolify()
        fake = FakeCoolify()
        d = self._deploy(fake)
        conn = self.app_conn()
        # Someone re-pointed the uuid at an unrelated Coolify application.
        fake.app_name = "production-billing-api"
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            engine.expire_deployment(conn, self.app.config, {"deployment_id": d["id"]}, lambda *a, **k: True)
        self.assertNotIn("DELETE", [c["method"] for c in fake.calls])
        note = conn.execute("SELECT teardown_note FROM deployments WHERE id = ?", (d["id"],)).fetchone()[0]
        self.assertIn("Refused to delete", note)

    def test_non_live_deployment_expires_without_touching_coolify(self):
        self.configure_coolify()
        fake = FakeCoolify()
        first = self._deploy(fake)
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            self.call("post", "/api/projects/web-app/deploy")
            self.work()
        conn = self.app_conn()
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            engine.expire_deployment(conn, self.app.config, {"deployment_id": first["id"]}, lambda *a, **k: True)
        self.assertEqual(fake.calls, [], "superseded deployments must not tear down the live app")
        status = conn.execute("SELECT status FROM deployments WHERE id = ?", (first["id"],)).fetchone()[0]
        self.assertEqual(status, "expired")


class CoolifyAdminTests(CoolifyCase):
    def test_connection_test_lists_servers_projects_and_keys(self):
        self.configure_coolify()
        self.login(ADMIN[0], ADMIN[2])
        with mock.patch("kova.engine.requests.request", side_effect=FakeCoolify()):
            r = self.call("post", "/api/admin/integrations/coolify/test")
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["servers"][0]["uuid"], "srv-1")
        self.assertEqual(data["private_keys"][0]["uuid"], "key-1")

    def test_connection_failure_is_reported_honestly(self):
        self.configure_coolify()
        self.login(ADMIN[0], ADMIN[2])
        import requests as rq
        with mock.patch("kova.engine.requests.request", side_effect=rq.ConnectionError("x")):
            r = self.call("post", "/api/admin/integrations/coolify/test")
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()["ok"])

    def test_token_is_never_returned_to_the_browser(self):
        self.configure_coolify()
        self.login(ADMIN[0], ADMIN[2])
        body = self.client.get("/api/admin/integrations").get_data(as_text=True)
        self.assertNotIn("secret-coolify-token", body)
        self.assertIn("token_set", body)

    def test_half_configured_coolify_falls_back_to_the_simulator(self):
        self.configure_coolify(coolify_server_uuid="")
        self.as_student()
        d = self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(d["provider"], "simulator")
        self.assertTrue(d["is_simulated"])
