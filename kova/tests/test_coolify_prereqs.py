"""Coolify prerequisites (COOLIFY_SINGLE_VPS_PLAN.md §7): on-demand TLS approval,
edge-TLS hostnames, durable credential revocation and app teardown retries.

All against mocked Coolify/authz servers (no live Coolify exists yet).
"""
import os
from unittest import mock

from kova import engine, jobs
from kova.db import connect, set_setting

import shutil
import tempfile

# Module imports (not classes) so pytest does not collect those TestCases here again.
from . import test_authz_integration as tai
from .test_coolify import CoolifyCase, FakeCoolify

WEBSITE_KEY, FakeAuthz, Router = tai.WEBSITE_KEY, tai.FakeAuthz, tai.Router


class AuthzCase(CoolifyCase):
    """EngineAuthorizationTests' fixtures and helpers WITHOUT inheriting its tests."""
    linked_project = tai.EngineAuthorizationTests.linked_project
    deploy = tai.EngineAuthorizationTests.deploy

    def setUp(self):                     # same as EngineAuthorizationTests.setUp
        super().setUp()
        self.secret_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.secret_dir, True)
        self.app.config.update(AUTHZ_URL="http://authz.test:8010", AUTHZ_WEBSITE_KEY=WEBSITE_KEY)
        self.configure_coolify()
        self.authz, self.coolify = FakeAuthz(), FakeCoolify()
        self.router = Router(self.authz, self.coolify)

DEP_REF = "dep_abcdef0123456789"
BASE = "apps.example.com"


class FlakyCoolify(FakeCoolify):
    """FakeCoolify whose DELETE /services/<uuid> can be made to fail."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.delete_fails = False

    def _route(self, method, path, kw):
        if method == "DELETE" and self.delete_fails:
            return {"message": "Coolify is restarting"}, 500
        return super()._route(method, path, kw)


def _make_jobs_due(conn):
    conn.execute("UPDATE jobs SET run_after = '2000-01-01T00:00:00+00:00' WHERE state = 'pending'")
    conn.commit()


def _jobs(conn, kind):
    return conn.execute("SELECT * FROM jobs WHERE kind = ?", (kind,)).fetchall()


NOBEAT = lambda *a, **k: True  # noqa: E731


# ---------------------------------------------------------------- TLS approval

class TlsAskTests(CoolifyCase):
    def _live(self, fake=None):
        self.configure_coolify()
        self.as_student()
        with mock.patch("kova.engine.requests.request", side_effect=fake or FakeCoolify()):
            d = self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(d["status"], "ready", d)
        return d

    def ask(self, domain, **headers):
        return self.client.get("/internal/tls-ask", query_string={"domain": domain}, headers=headers).status_code

    def test_approves_only_the_live_app_hostname(self):
        self._live()
        self.assertEqual(self.ask(f"web-app.{BASE}"), 200)
        self.assertEqual(self.ask(f"WEB-APP.{BASE}."), 200, "case and trailing dot are normalised")
        for bad in (f"other.{BASE}", f"x.web-app.{BASE}", BASE, f"web-app.{BASE}.evil.com",
                    "web-app.evil.com", "", "web_app." + BASE, "a" * 300 + "." + BASE):
            self.assertEqual(self.ask(bad), 404, bad)

    def test_refuses_requests_that_came_through_the_public_proxy(self):
        self._live()
        self.assertEqual(self.ask(f"web-app.{BASE}", **{"X-Forwarded-For": "203.0.113.9"}), 404)
        self.assertEqual(self.ask(f"web-app.{BASE}", Forwarded="for=203.0.113.9"), 404)

    def test_expired_app_gets_no_certificate(self):
        d = self._live()
        with mock.patch("kova.engine.requests.request", side_effect=FakeCoolify()):
            engine.expire_deployment(self.app_conn(), self.app.config, {"deployment_id": d["id"]}, NOBEAT)
        self.assertEqual(self.ask(f"web-app.{BASE}"), 404)

    def test_ready_but_past_ttl_gets_no_certificate(self):
        d = self._live()
        conn = self.app_conn()
        conn.execute("UPDATE deployments SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (d["id"],))
        conn.commit()
        self.assertEqual(self.ask(f"web-app.{BASE}"), 404)

    def test_failed_build_and_simulator_get_no_certificate(self):
        self.configure_coolify()
        self.as_student()
        with mock.patch("kova.engine.requests.request",
                        side_effect=FakeCoolify(app_statuses=["starting:unhealthy", "exited"])):
            self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(self.ask(f"web-app.{BASE}"), 404)

    def test_simulated_deployment_gets_no_certificate(self):
        conn = connect(self.app.config["DATABASE"])
        set_setting(conn, "coolify_base_domain", BASE)
        conn.close()
        self.as_student()
        d = self.wait(self.create().get_json()["deployment_id"])     # simulator (Coolify not configured)
        self.assertEqual(d["status"], "ready")
        self.assertEqual(self.ask(f"web-app.{BASE}"), 404)


# ---------------------------------------------------------------- edge TLS hostnames

class EdgeTlsDomainTests(CoolifyCase):
    def _deploy(self, fake, **settings):
        self.configure_coolify(**settings)
        self.as_student()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            return self.wait(self.create().get_json()["deployment_id"])

    @staticmethod
    def _urls(fake):
        return next(c for c in fake.calls if (c["method"], c["path"]) == ("POST", "/services"))["json"]["urls"]

    def test_edge_tls_gives_coolify_http_and_students_https(self):
        fake = FakeCoolify()
        d = self._deploy(fake, coolify_tls_at_edge="1")
        # Coolify/Traefik only ever see http://, so Traefik neither requests a certificate nor redirects.
        self.assertEqual(self._urls(fake), [{"name": "app", "url": f"http://web-app.{BASE}:3000"}])
        self.assertEqual(d["url"], f"https://web-app.{BASE}")

    def test_without_edge_tls_behaviour_is_unchanged(self):
        fake = FakeCoolify()
        d = self._deploy(fake)
        self.assertEqual(self._urls(fake), [{"name": "app", "url": f"https://web-app.{BASE}:3000"}])
        self.assertEqual(d["url"], "https://web-app.apps.example.com")

    def test_app_domains_helper(self):
        s = {"base_domain": BASE, "tls_at_edge": True}
        self.assertEqual(engine.app_domains("x", s), (f"http://x.{BASE}", f"https://x.{BASE}"))
        self.assertEqual(engine.app_domains("x", {"base_domain": ""}), (None, None))
        self.assertEqual(engine.app_domains("x", {"base_domain": BASE, "force_https": False}),
                         (f"http://x.{BASE}", f"http://x.{BASE}"))


class ComposeDomainTests(AuthzCase):
    def test_compose_routes_only_the_app_service(self):
        self.app.config["COOLIFY_TLS_AT_EDGE"] = True
        slug = self.linked_project()
        d = self.deploy(slug)
        self.assertEqual(d["status"], "ready", d)
        create = [c for c in self.coolify.calls if (c["method"], c["path"]) == ("POST", "/services")]
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0]["json"]["urls"], [{"name": "app", "url": f"http://{slug}.{BASE}:3000"}])
        self.assertEqual(d["url"], f"https://{slug}.{BASE}")


# ---------------------------------------------------------------- revocation retries

class RevokeRetryTests(AuthzCase):
    def setUp(self):
        AuthzCase.setUp(self)
        self.app.config["RUNTIME_SECRET_DIR"] = self.secret_dir

    def _ready(self):
        slug = self.linked_project()
        d = self.deploy(slug)
        self.assertEqual(d["status"], "ready", d)
        # Coolify builds request no credential any more; these tests cover a credential
        # an earlier (sidecar) build left behind, which must still be revoked.
        from kova import runtime_secrets
        conn = self.app_conn()
        conn.execute("UPDATE deployments SET authz_deployment_id = ? WHERE id = ?", (DEP_REF, d["id"]))
        conn.commit()
        runtime_secrets.deliver(self.app.config, DEP_REF, "csr_left-by-an-earlier-build")
        self.assertTrue(os.path.exists(os.path.join(self.secret_dir, DEP_REF, "runtime_secret")))
        return slug, d

    def _expire(self, d):
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            engine.expire_deployment(self.app_conn(), self.app.config, {"deployment_id": d["id"]}, NOBEAT)

    def _run_due_jobs(self):
        _make_jobs_due(self.app_conn())
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            self.work()

    def test_expiry_with_authz_down_destroys_secret_now_and_revokes_later(self):
        _, d = self._ready()
        self.authz.down = True
        self._expire(d)
        conn = self.app_conn()
        self.assertFalse(os.path.exists(os.path.join(self.secret_dir, DEP_REF)),
                         "local kill switch: the sidecar's secret file must go immediately")
        row = conn.execute("SELECT status, authz_revoked_at, teardown_note FROM deployments WHERE id = ?",
                           (d["id"],)).fetchone()
        self.assertEqual(row["status"], "expired")
        self.assertIsNone(row["authz_revoked_at"])
        self.assertIn("retried automatically", row["teardown_note"])
        self.assertEqual(len(_jobs(conn, "authz_revoke")), 1)

        self.authz.down = False
        self._run_due_jobs()
        self.assertIn(DEP_REF, self.authz.revoked)
        self.assertIsNotNone(conn.execute("SELECT authz_revoked_at FROM deployments WHERE id = ?",
                                          (d["id"],)).fetchone()[0])

    def test_retry_keeps_backing_off_while_authz_stays_down(self):
        _, d = self._ready()
        self.authz.down = True
        self._expire(d)
        for _ in range(3):
            self._run_due_jobs()
        job = _jobs(self.app_conn(), "authz_revoke")[0]
        self.assertEqual(job["state"], "pending", "must not give up after a few failures")
        self.assertGreaterEqual(job["attempts"], 3)
        self.assertEqual(job["max_attempts"], engine.REVOKE_RETRY_ATTEMPTS)

    def test_credential_is_revoked_before_the_app_teardown(self):
        _, d = self._ready()
        order = []
        real_revoke, real_teardown = engine._revoke_remote, engine.teardown_coolify_app
        with mock.patch.object(engine, "_revoke_remote", side_effect=lambda *a: (order.append("revoke"),
                                                                                  real_revoke(*a))[1]), \
                mock.patch.object(engine, "teardown_coolify_app",
                                  side_effect=lambda *a: (order.append("teardown"), real_teardown(*a))[1]):
            self._expire(d)
        self.assertEqual(order, ["revoke", "teardown"])

    def test_force_deleted_project_is_still_revoked_later(self):
        slug, _ = self._ready()
        self.authz.down = True
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            r = self.client.delete(f"/api/projects/{slug}", json={"confirm": slug, "force": True},
                                   headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.status_code, 200)
        conn = self.app_conn()
        self.assertIsNone(conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone())
        self.assertEqual(len(_jobs(conn, "authz_revoke")), 1, "the job carries the credential id itself")
        self.authz.down = False
        self._run_due_jobs()
        self.assertIn(DEP_REF, self.authz.revoked)

    def test_sweep_catches_up_on_unrevoked_finished_deployments(self):
        _, d = self._ready()
        conn = self.app_conn()
        # e.g. a row left by older code: over, credential never revoked, no job queued
        conn.execute("UPDATE deployments SET status = 'failed' WHERE id = ?", (d["id"],))
        conn.commit()
        self.assertEqual(_jobs(conn, "authz_revoke"), [])
        jobs.sweep(conn, self.app.config)
        jobs.sweep(conn, self.app.config)                 # idempotent: still one job
        self.assertEqual(len(_jobs(conn, "authz_revoke")), 1)
        self._run_due_jobs()
        self.assertIn(DEP_REF, self.authz.revoked)

    def test_live_deployment_is_never_swept(self):
        self._ready()
        jobs.sweep(self.app_conn(), self.app.config)
        self.assertEqual(_jobs(self.app_conn(), "authz_revoke"), [])
        self.assertEqual(self.authz.revoked, [])


# ---------------------------------------------------------------- teardown retries

class TeardownRetryTests(CoolifyCase):
    def _live(self, fake):
        self.configure_coolify()
        self.as_student()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            d = self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(d["status"], "ready")
        return d

    def _expire(self, fake, d):
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            engine.expire_deployment(self.app_conn(), self.app.config, {"deployment_id": d["id"]}, NOBEAT)

    def _run_due(self, fake):
        _make_jobs_due(self.app_conn())
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            self.work()

    def _provider_ref(self):
        return self.app_conn().execute("SELECT provider_ref FROM projects WHERE slug = 'web-app'").fetchone()[0]

    def test_failed_delete_at_expiry_is_retried_until_the_app_is_gone(self):
        fake = FlakyCoolify()
        d = self._live(fake)
        fake.delete_fails = True
        self._expire(fake, d)
        conn = self.app_conn()
        note = conn.execute("SELECT teardown_note FROM deployments WHERE id = ?", (d["id"],)).fetchone()[0]
        self.assertIn("Removal will be retried automatically", note)
        self.assertEqual(self._provider_ref(), "svc-123", "app may still be serving")
        self.assertEqual(len(_jobs(conn, "coolify_teardown")), 1)

        self._run_due(fake)                                  # still failing: backs off
        self.assertEqual(_jobs(conn, "coolify_teardown")[0]["state"], "pending")
        fake.delete_fails = False
        fake.calls.clear()
        self._run_due(fake)
        self.assertIn(("DELETE", "/services/svc-123"), [(c["method"], c["path"]) for c in fake.calls])
        self.assertIsNone(self._provider_ref())

    def test_retry_never_deletes_an_app_a_newer_deployment_is_using(self):
        fake = FlakyCoolify()
        d = self._live(fake)
        fake.delete_fails = True
        self._expire(fake, d)
        fake.delete_fails = False
        with mock.patch("kova.engine.requests.request", side_effect=fake):   # student redeploys: live again
            self.call("post", "/api/projects/web-app/deploy")
            self.work()
        fake.calls.clear()
        self._run_due(fake)
        self.assertNotIn("DELETE", [c["method"] for c in fake.calls])
        self.assertEqual(self._provider_ref(), "svc-123")

    def test_name_guard_refusal_is_not_retried(self):
        fake = FlakyCoolify()
        d = self._live(fake)
        fake.app_name = "someone-elses-app"
        self._expire(fake, d)
        self.assertEqual(_jobs(self.app_conn(), "coolify_teardown"), [])

    def test_sweep_requeues_an_expired_app_that_is_still_present(self):
        fake = FlakyCoolify()
        d = self._live(fake)
        conn = self.app_conn()
        # state left by older code: expired, app still attached, no job
        conn.execute("UPDATE deployments SET status = 'expired', expired_at = '2026-11-12T00:00:00+00:00' "
                     "WHERE id = ?", (d["id"],))
        conn.execute("UPDATE projects SET current_deployment_id = NULL WHERE slug = 'web-app'")
        conn.commit()
        jobs.sweep(conn, self.app.config)
        jobs.sweep(conn, self.app.config)
        self.assertEqual(len(_jobs(conn, "coolify_teardown")), 1)
        self._run_due(fake)
        self.assertIsNone(self._provider_ref())

    def test_sweep_ignores_live_apps(self):
        fake = FlakyCoolify()
        self._live(fake)
        jobs.sweep(self.app_conn(), self.app.config)
        self.assertEqual(_jobs(self.app_conn(), "coolify_teardown"), [])
