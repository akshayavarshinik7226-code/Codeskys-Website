"""Information disclosure: what a student can read about the infrastructure.

Students must never receive the backend product name, internal host names, IP
addresses, server paths or tokens: not in API responses, deployment errors or the
build log. Administrators keep the unredacted detail.
"""
import json
import unittest
from unittest import mock

import requests as rq

from kova import jobs
from kova.security import redact

from .base import ADMIN, STUDENT, AppTestCase
from .test_coolify import CoolifyCase, FakeCoolify

# What a failing Coolify could put in an error body.
LEAKY_BODY = {"message": "SQLSTATE[08006] could not connect to coolify-db:5432 (10.0.1.5), "
                         "see /data/coolify/source/.env; Authorization: Bearer 3|AbCdEfGhIjKlMnOpQrStUvWx "
                         "via http://coolify:8080/api/v1/services on [fd00::1]"}
LEAKS = ("coolify", "10.0.1.5", "5432", "/data/", "AbCdEfGhIjKlMnOpQrStUvWx", "fd00::1", "http://",
         "8080", "secret-coolify-token")


class RedactTests(unittest.TestCase):
    def test_infrastructure_details_are_removed(self):
        cases = {
            "HTTPConnectionPool(host='coolify', port=8080): Max retries exceeded": "coolify",
            "connect to 129.121.169.143:22 failed": "129.121.169.143",
            "listen fd00::1 and 2001:db8:0:0:0:0:0:1": "fd00::1",
            "upstream coolify-proxy:80 refused": "coolify-proxy",
            "token Bearer 12|abcdefghijklmnopqrstuvwxyz0123": "abcdefghijklmnopqrstuvwxyz0123",
            "file /etc/codeskys/secrets/coolify_token": "/etc/codeskys",
            "see https://internal.example/api": "internal.example",
            "key " + "a" * 48: "a" * 48,
        }
        for text, secret in cases.items():
            with self.subTest(text=text):
                self.assertNotIn(secret, redact(text))
                self.assertNotIn("coolify", redact(text).lower())

    def test_ordinary_messages_are_kept(self):
        for text in ("The deployment engine returned 404: Service not found.",
                     "the application is running:healthy", "at 12:30:45 on 2026-11-13T10:00:00Z",
                     "Refusing to touch app service svc-123: it does not carry the Codeskys ownership marker"):
            with self.subTest(text=text):
                self.assertEqual(redact(text), text)


class StatusDisclosureTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(COOLIFY_URL="http://coolify:8080", COOLIFY_TOKEN="secret-coolify-token")

    def test_students_get_neutral_engine_fields_only(self):
        self.login(STUDENT[0], STUDENT[2])
        r = self.client.get("/api/platform/status")
        data = r.get_json()
        self.assertEqual(data["engine"], {"configured": True, "state": "unchecked"})
        self.assertNotIn("coolify", data, "no backend-specific block for students")
        body = r.get_data(as_text=True)
        self.assertNotIn("coolify:8080", body)
        self.assertNotIn("secret-coolify-token", body)

    def test_admins_keep_the_internal_host(self):
        self.login(ADMIN[0], ADMIN[2])
        data = self.client.get("/api/platform/status").get_json()
        self.assertEqual(data["coolify"]["host"], "coolify:8080")
        self.assertEqual(data["engine"]["state"], "unchecked")
        self.assertNotIn("secret-coolify-token", json.dumps(data))


class EngineErrorDisclosureTests(CoolifyCase):
    def setUp(self):
        super().setUp()
        self.configure_coolify(coolify_url="http://coolify:8080")

    def assert_clean(self, text):
        for leak in LEAKS:
            self.assertNotIn(leak, text.lower() if leak == "coolify" else text)

    def test_a_raw_coolify_error_body_is_redacted_for_the_student(self):
        self.as_student()
        fake = FakeCoolify()
        fake.fail[("POST", "/services")] = (500, LEAKY_BODY)
        d = self.deploy_with(fake)
        self.assertEqual(d["status"], "failed")
        self.assertIn("The deployment engine returned 500", d["error"])
        self.assert_clean(d["error"])
        self.assert_clean(" ".join(self.logs(d["id"])))
        full = self.client.get(f"/api/deployments/{d['id']}").get_json()
        # "provider" is an internal compatibility value the UI branches on, never shown as text.
        self.assertEqual(full["deployment"].pop("provider"), "coolify")
        self.assert_clean(json.dumps(full))

    def test_an_unreachable_engine_does_not_reveal_its_address(self):
        self.as_student()
        def down(method, url, **kw):
            if "/internal/website" in url:
                return FakeCoolify()(method, url, **kw)
            raise rq.ConnectionError("HTTPConnectionPool(host='coolify', port=8080): Max retries exceeded "
                                     "with url: /api/v1/services (Caused by NewConnectionError(10.0.1.5))")
        d = self.deploy_with(down)
        self.assertEqual(d["status"], "failed")
        self.assert_clean(d["error"])
        self.assert_clean(" ".join(self.logs(d["id"])))

    def test_app_control_errors_are_redacted(self):
        self.as_student()
        fake = FakeCoolify()
        self.assertEqual(self.deploy_with(fake)["status"], "ready")
        fake.fail[("POST", "/services/svc-123/stop")] = (500, LEAKY_BODY)
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.call("post", "/api/projects/web-app/app/stop")
        self.assertEqual(r.status_code, 502)
        self.assertIn("could not stop the application", r.get_json()["error"])
        self.assert_clean(r.get_data(as_text=True))

    def test_an_address_coolify_generated_itself_is_not_shown(self):
        self.as_student()
        fake = FakeCoolify()
        self.assertEqual(self.deploy_with(fake)["status"], "ready")
        fake.services["svc-123"]["fqdn"]["app"] = "http://svc-123.129.121.169.143.sslip.io"
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.client.get("/api/projects/web-app/app")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.get_json()["fqdn"])
        self.assertNotIn("129.121.169.143", r.get_data(as_text=True))

    def test_admins_keep_the_unredacted_detail(self):
        self.login(ADMIN[0], ADMIN[2])
        fake = FakeCoolify()
        fake.fail[("GET", "/version")] = (500, LEAKY_BODY)
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.call("post", "/api/admin/integrations/coolify/test")
        self.assertEqual(r.status_code, 502)
        self.assertIn("coolify-db:5432", r.get_json()["error"])


class GiveUpDisclosureTests(CoolifyCase):
    def test_an_unexpected_worker_error_is_redacted_on_the_deployment(self):
        self.as_student()
        dep_id = self.create().get_json()["deployment_id"]
        conn = self.app_conn()
        conn.execute("UPDATE deployments SET status = 'building' WHERE id = ?", (dep_id,))
        conn.commit()
        job = {"kind": "deploy", "attempts": 3, "payload": json.dumps({"deployment_id": dep_id})}
        jobs._give_up(conn, job, "ConnectionError: HTTPConnectionPool(host='coolify', port=8080): "
                                 "url http://coolify:8080/api/v1/services from 10.0.1.5")
        d = self.client.get(f"/api/deployments/{dep_id}").get_json()["deployment"]
        self.assertEqual(d["status"], "failed")
        self.assertIn("Build gave up after 3 attempts", d["error"])
        for leak in ("coolify", "10.0.1.5", "8080"):
            self.assertNotIn(leak, d["error"].lower())
