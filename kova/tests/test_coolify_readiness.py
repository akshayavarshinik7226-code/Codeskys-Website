"""Readiness, status synchronisation and the expired-app gate (Coolify, mocked).

A deployment is "ready" only after Coolify's build finished AND the application
runs AND its URL answers; the 24-hour window starts then. Expired apps are
refused at the edge (/internal/app-gate) whether or not their teardown worked.
Like the other Coolify tests these prove the contract, not live behaviour.
"""
from datetime import datetime, timezone
from unittest import mock

from kova import engine
from kova.db import connect

from .test_coolify import CoolifyCase, FakeCoolify
from .test_coolify_prereqs import BASE, NOBEAT

PATCH = "kova.engine.requests.request"


class ReadinessCase(CoolifyCase):
    def setUp(self):
        super().setUp()
        self.app.config["COOLIFY_READY_TIMEOUT"] = 1
        self.configure_coolify()
        self.as_student()

    def deploy(self, fake):
        with mock.patch(PATCH, side_effect=fake):
            return self.wait(self.create().get_json()["deployment_id"])

    def logs(self, dep_id):
        conn = self.app_conn()
        return [r["line"] for r in conn.execute("SELECT line FROM deploy_logs WHERE deployment_id = ? ORDER BY id",
                                                (dep_id,))]


class ExpiryWindowTests(ReadinessCase):
    def test_window_is_exactly_24_hours_from_the_verified_start_then_the_sweep_ends_it(self):
        from kova import jobs
        fake = FakeCoolify()
        d = self.deploy(fake)
        self.assertEqual(d["status"], "ready", d)
        conn = self.app_conn()
        row = conn.execute("SELECT finished_at, expires_at FROM deployments WHERE id = ?", (d["id"],)).fetchone()
        window = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["finished_at"])
        self.assertAlmostEqual(window.total_seconds(), 24 * 3600, delta=5)
        self.assertEqual(self.client.get("/internal/app-gate", headers={"X-Forwarded-Host": f"web-app.{BASE}"})
                         .status_code, 200)
        # Shortened TTL for the test: the window ran out.
        conn.execute("UPDATE deployments SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (d["id"],))
        conn.commit()
        jobs.sweep(conn, self.app.config)
        with mock.patch(PATCH, side_effect=fake):
            self.work()
        row = conn.execute("SELECT status, teardown_note FROM deployments WHERE id = ?", (d["id"],)).fetchone()
        self.assertEqual(row["status"], "expired")
        self.assertIn(("DELETE", "/services/svc-123"), [(c["method"], c["path"]) for c in fake.calls])
        self.assertEqual(fake.uuids(), [])
        self.assertEqual(self.client.get("/internal/app-gate", headers={"X-Forwarded-Host": f"web-app.{BASE}"})
                         .status_code, 410)
        with mock.patch(PATCH, side_effect=fake):
            self.assertEqual(self.call("post", "/api/projects/web-app/app/start").status_code, 409)


class ReadinessTests(ReadinessCase):
    def test_ready_only_after_the_app_runs_and_answers(self):
        probes = []
        self.probe = lambda url, base="": probes.append((url, base)) or (True, "HTTP 200")
        before = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fake = FakeCoolify()
        fake.app_statuses = ["starting:unknown", "running:unknown", "running:healthy"]
        d = self.deploy(fake)
        self.assertEqual(d["status"], "ready", d)
        self.assertGreaterEqual(len(probes), 2, "two consecutive passing checks are required")
        self.assertEqual(probes[0][0], "https://web-app.apps.example.com")
        row = self.app_conn().execute("SELECT expires_at, finished_at FROM deployments WHERE id = ?",
                                      (d["id"],)).fetchone()
        self.assertGreaterEqual(row["finished_at"], before)
        self.assertTrue(row["expires_at"] > row["finished_at"], "the 24 h window starts once verified")
        self.assertTrue(any("Health check passed" in line for line in self.logs(d["id"])))

    def test_probe_goes_through_the_in_server_proxy_when_configured(self):
        self.app.config["COOLIFY_PROBE_URL"] = "http://coolify-proxy:80"
        probes = []
        self.probe = lambda url, base="": probes.append((url, base)) or (True, "HTTP 200")
        self.assertEqual(self.deploy(FakeCoolify())["status"], "ready")
        self.assertEqual(probes[0][1], "http://coolify-proxy:80")

    def test_crashing_app_fails_is_stopped_and_shows_its_output(self):
        fake = FakeCoolify(sensitive=True)            # app logs need read:sensitive
        fake.app_statuses = ["starting:unhealthy", "running:unknown", "exited:unhealthy"]
        fake.app_log_text = "ModuleNotFoundError: No module named 'joblib'"
        self.probe = lambda url, base="": (False, "HTTP 502")
        d = self.deploy(fake)
        self.assertEqual(d["status"], "failed")
        self.assertIn("did not become available", d["error"])
        self.assertIn("exited:unhealthy", d["error"])
        self.assertIn(("GET", "/services/svc-123/logs"), [(c["method"], c["path"]) for c in fake.calls])
        self.assertTrue(any("joblib" in line for line in self.logs(d["id"])))
        row = self.app_conn().execute("SELECT expires_at FROM deployments WHERE id = ?", (d["id"],)).fetchone()
        self.assertFalse(row["expires_at"], "a failed deployment gets no availability window")
        self.assertEqual(self.client.get(f"/internal/tls-ask?domain=web-app.{BASE}").status_code, 404)

    def test_running_app_that_fails_its_check_is_stopped(self):
        fake = FakeCoolify()                          # the Codeskys token: no read:sensitive
        self.probe = lambda url, base="": (False, "HTTP 502")
        d = self.deploy(fake)
        self.assertEqual(d["status"], "failed")
        self.assertIn(("POST", "/services/svc-123/stop"), [(c["method"], c["path"]) for c in fake.calls])
        self.assertTrue(fake.services["svc-123"]["status"].startswith("exited"))
        self.assertFalse(any("Application output" in line for line in self.logs(d["id"])),
                         "Coolify refuses the logs; nothing is invented")

    def test_app_that_starts_then_crash_loops_is_not_live(self):
        fake = FakeCoolify()
        fake.app_statuses = ["starting:unhealthy", "running:unknown"] + ["restarting:unknown", "running:unknown"] * 200
        d = self.deploy(fake)
        self.assertEqual(d["status"], "failed", "never two passing checks in a row")

    def test_url_that_does_not_answer_fails_the_deployment(self):
        for detail in ("no answer (ConnectTimeout)", "HTTP 502", "the proxy has no route to the app yet"):
            with self.subTest(detail=detail):
                self.probe = lambda url, base="", d=detail: (False, d)
                conn = self.app_conn()
                conn.execute("DELETE FROM deployments")
                conn.execute("DELETE FROM projects")
                conn.commit()
                d = self.deploy(FakeCoolify())
                self.assertEqual(d["status"], "failed")
                self.assertIn(detail, d["error"])

    def test_no_public_address_is_never_reported_ready(self):
        conn = connect(self.app.config["DATABASE"])
        conn.execute("UPDATE settings SET value = '' WHERE key = 'coolify_base_domain'")
        conn.commit()
        conn.close()
        fake = FakeCoolify()
        d = self.deploy(fake)
        self.assertEqual(d["status"], "failed")
        self.assertIn("no public address", d["error"])
        self.assertFalse([c for c in fake.calls if c["path"].startswith("/services")], "nothing is built")

    def test_cancel_during_the_health_check_cancels_and_stops_the_app(self):
        conn = self.app_conn()

        def probe(url, base=""):
            dep = conn.execute("SELECT id FROM deployments ORDER BY id DESC LIMIT 1").fetchone()
            engine.request_cancel(conn, dep["id"])
            return True, "HTTP 200"
        self.probe = probe
        fake = FakeCoolify()
        d = self.deploy(fake)
        self.assertEqual(d["status"], "cancelled")
        self.assertIn(("POST", "/services/svc-123/stop"), [(c["method"], c["path"]) for c in fake.calls])


class StatusSyncTests(ReadinessCase):
    def test_a_short_coolify_outage_does_not_fail_a_running_build(self):
        fake = FakeCoolify()
        reads = {"n": 0, "started": False}

        def flaky(method, url, **kw):
            if url.endswith("/start"):
                reads["started"] = True
            elif reads["started"] and method == "GET" and url.endswith("/services/svc-123"):
                reads["n"] += 1
                if reads["n"] <= 3:
                    raise engine.requests.ConnectionError("blip")
            return fake(method, url, **kw)
        d = self.deploy(flaky)
        self.assertEqual(d["status"], "ready", d)
        self.assertTrue(any("retrying" in line for line in self.logs(d["id"])))

    def test_a_lasting_coolify_outage_fails_with_a_clear_message(self):
        fake = FakeCoolify()
        state = {"started": False}

        def down(method, url, **kw):
            if url.endswith("/start"):
                state["started"] = True
            elif state["started"]:
                raise engine.requests.ConnectionError("down")
            return fake(method, url, **kw)
        d = self.deploy(down)
        self.assertEqual(d["status"], "failed")
        self.assertIn("Lost contact with the deployment engine", d["error"])

    def test_cancel_is_retried_when_coolify_refuses_it_once(self):
        fake = FakeCoolify(app_statuses=["starting:unhealthy"])
        stops, state = [], {"started": False}

        conn = self.app_conn()

        def once(method, url, **kw):
            path = url.split("?")[0]
            if path.endswith("/start"):
                state["started"] = True
            elif path.endswith("/stop"):
                stops.append(method)
                if len(stops) == 1:
                    raise engine.requests.ConnectionError("blip")
            elif state["started"] and method == "GET":
                # The student presses Cancel while Coolify is building.
                engine.request_cancel(conn, conn.execute("SELECT MAX(id) FROM deployments").fetchone()[0])
            return fake(method, url, **kw)
        d = self.deploy(once)
        self.assertEqual(d["status"], "cancelled")
        self.assertEqual(len(stops), 2)
        self.assertTrue(fake.services["svc-123"]["status"].startswith("exited"))

    def test_worker_restart_after_the_build_resumes_into_the_health_check(self):
        fake = FakeCoolify()
        d = self.deploy(fake)
        conn = self.app_conn()
        # As if the worker died after Coolify finished but before Codeskys verified.
        conn.execute("UPDATE deployments SET status = 'building', stage = 'deploying', expires_at = NULL, "
                     "finished_at = NULL WHERE id = ?", (d["id"],))
        conn.execute("UPDATE projects SET current_deployment_id = NULL")
        conn.commit()
        probes = []
        self.probe = lambda url, base="": probes.append(url) or (True, "HTTP 200")
        with mock.patch(PATCH, side_effect=fake):
            engine.run_deployment(conn, self.app.config, {"deployment_id": d["id"]}, NOBEAT)
        row = conn.execute("SELECT status, expires_at FROM deployments WHERE id = ?", (d["id"],)).fetchone()
        self.assertEqual(row["status"], "ready")
        self.assertTrue(row["expires_at"])
        self.assertTrue(probes, "the resumed deployment is verified again")


class AppControlLivenessTests(ReadinessCase):
    def test_expired_or_failed_apps_cannot_be_started_again(self):
        fake = FakeCoolify()
        d = self.deploy(fake)
        conn = self.app_conn()
        conn.execute("UPDATE deployments SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (d["id"],))
        conn.commit()
        with mock.patch(PATCH, side_effect=fake):
            for action in ("start", "restart"):
                r = self.call("post", f"/api/projects/web-app/app/{action}")
                self.assertEqual(r.status_code, 409, action)
                self.assertIn("no live deployment", r.get_json()["error"])
            self.assertEqual(self.call("post", "/api/projects/web-app/app/stop").status_code, 200,
                             "stopping is always allowed")


class AppGateTests(ReadinessCase):
    def gate(self, host, **headers):
        return self.client.get("/internal/app-gate", headers={"X-Forwarded-Host": host, **headers})

    def test_live_app_passes(self):
        self.deploy(FakeCoolify())
        r = self.gate(f"web-app.{BASE}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.gate(f"WEB-APP.{BASE}:443").status_code, 200, "case and port are normalised")

    def test_expired_app_gets_the_branded_page(self):
        d = self.deploy(FakeCoolify())
        with mock.patch(PATCH, side_effect=FakeCoolify()):
            engine.expire_deployment(self.app_conn(), self.app.config, {"deployment_id": d["id"]}, NOBEAT)
        r = self.gate(f"web-app.{BASE}")
        self.assertEqual(r.status_code, 410)
        html = r.get_data(as_text=True)
        self.assertIn("Deployment expired", html)
        self.assertIn("Web App", html)
        self.assertIn("Codeskys", html)
        self.assertNotIn("/static/", html, "the page is self-contained (served on the app's hostname)")
        self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_past_ttl_is_refused_before_the_expiry_job_runs(self):
        d = self.deploy(FakeCoolify())
        conn = self.app_conn()
        conn.execute("UPDATE deployments SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (d["id"],))
        conn.commit()
        self.assertEqual(self.gate(f"web-app.{BASE}").status_code, 410)

    def test_refused_even_when_the_coolify_teardown_failed(self):
        fake = FakeCoolify()
        d = self.deploy(fake)

        def no_delete(method, url, **kw):
            if method == "DELETE":
                raise engine.requests.ConnectionError("coolify down")
            return fake(method, url, **kw)
        conn = self.app_conn()
        with mock.patch(PATCH, side_effect=no_delete):
            engine.expire_deployment(conn, self.app.config, {"deployment_id": d["id"]}, NOBEAT)
        self.assertTrue(conn.execute("SELECT provider_ref FROM projects WHERE slug = 'web-app'").fetchone()[0],
                        "the app is still on Coolify (teardown pending)")
        self.assertEqual(self.gate(f"web-app.{BASE}").status_code, 410)

    def test_failed_first_build_and_unknown_hosts_are_not_served(self):
        self.probe = lambda url, base="": (False, "HTTP 502")
        self.deploy(FakeCoolify())
        r = self.gate(f"web-app.{BASE}")
        self.assertEqual(r.status_code, 404)
        self.assertIn("No live deployment", r.get_data(as_text=True))
        for bad in (f"other.{BASE}", f"x.web-app.{BASE}", "web-app.evil.com", "", BASE):
            self.assertEqual(self.gate(bad).status_code, 404, bad)


class ProbeTests(CoolifyCase):
    """engine.probe_app itself (CoolifyCase stubs it for the deployment tests)."""

    def setUp(self):
        super().setUp()
        mock.patch.stopall()                     # use the real probe_app here

    def _get(self, status, text=""):
        resp = mock.Mock(status_code=status, text=text)
        return mock.patch("kova.engine.requests.get", return_value=resp)

    def test_answers_and_failures(self):
        cases = [(200, "", True), (302, "", True), (404, "Not Found: /", True), (401, "", True),
                 (404, "404 page not found\n", False), (502, "Bad Gateway", False), (503, "", False)]
        for status, text, ok in cases:
            with self.subTest(status=status, text=text), self._get(status, text):
                self.assertEqual(engine.probe_app("https://web-app.apps.example.com")[0], ok)

    def test_connection_error_is_a_failure(self):
        with mock.patch("kova.engine.requests.get", side_effect=engine.requests.ConnectTimeout()):
            ok, detail = engine.probe_app("https://web-app.apps.example.com")
        self.assertFalse(ok)
        self.assertIn("ConnectTimeout", detail)

    def test_in_server_probe_sends_the_app_host_and_never_follows_redirects(self):
        with self._get(200) as get:
            engine.probe_app("https://web-app.apps.example.com", "http://coolify-proxy:80")
        args, kw = get.call_args
        self.assertEqual(args[0], "http://coolify-proxy:80/")
        self.assertEqual(kw["headers"]["Host"], "web-app.apps.example.com")
        self.assertFalse(kw["allow_redirects"])
