"""Admin dashboard endpoints, authorization boundaries and rate limits."""
from datetime import datetime, timedelta, timezone

from kova import jobs
from kova.db import connect
from kova.ratelimit import rate_limit, reset

from .base import ADMIN, STUDENT, AppTestCase
from .test_platform import PlatformCase


class AdminDashboardTests(PlatformCase):
    def setUp(self):
        super().setUp()
        self.as_student()
        self.wait(self.create().get_json()["deployment_id"])
        self.post("/api/auth/logout")
        self.login(ADMIN[0], ADMIN[2])

    def test_overview_reports_platform_state(self):
        d = self.client.get("/api/admin/overview").get_json()
        self.assertEqual(d["stats"]["students"], 1)
        self.assertEqual(d["stats"]["projects"], 1)
        self.assertGreaterEqual(d["stats"]["deploys_30d"], 1)
        self.assertEqual(d["provider"], "simulator")
        self.assertIn("default_ttl_hours", d["settings"])
        self.assertEqual(len(d["expiring"]), 1)
        self.assertEqual(d["usage"][0]["projects"], 1)

    def test_deployments_can_be_filtered_by_status(self):
        rows = self.client.get("/api/admin/deployments").get_json()["deployments"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["owner"], "Sam Student")
        self.assertTrue(rows[0]["is_simulated"])
        self.assertEqual(self.client.get("/api/admin/deployments?status=failed").get_json()["deployments"], [])

    def test_projects_listing_shows_owners_and_limits(self):
        rows = self.client.get("/api/admin/projects").get_json()["projects"]
        self.assertEqual(rows[0]["owner_email"], STUDENT[0])
        self.assertEqual(rows[0]["limits_memory"], "512M")
        self.assertEqual(rows[0]["ttl_hours"], 24)

    def test_admin_can_expire_a_live_deployment(self):
        dep = self.client.get("/api/admin/deployments").get_json()["deployments"][0]
        r = self.call("post", f"/api/admin/deployments/{dep['id']}/expire")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["action"], "expired")
        after = self.client.get("/api/admin/deployments").get_json()["deployments"][0]
        self.assertEqual(after["status"], "expired")
        self.assertEqual(self.client.get("/sites/web-app/").status_code, 404)

    def test_expiring_twice_is_refused(self):
        dep = self.client.get("/api/admin/deployments").get_json()["deployments"][0]
        self.call("post", f"/api/admin/deployments/{dep['id']}/expire")
        again = self.call("post", f"/api/admin/deployments/{dep['id']}/expire")
        self.assertEqual(again.status_code, 409)

    def test_admin_can_stop_a_running_build(self):
        self.post("/api/auth/logout")
        self.as_student()
        dep_id = self.call("post", "/api/projects/web-app/deploy").get_json()["deployment"]["id"]
        self.post("/api/auth/logout")
        self.login(ADMIN[0], ADMIN[2])
        r = self.call("post", f"/api/admin/deployments/{dep_id}/expire")
        self.assertEqual(r.get_json()["action"], "cancelled")
        self.work()
        d = self.client.get(f"/api/deployments/{dep_id}").get_json()["deployment"]
        self.assertEqual(d["status"], "cancelled")

    def test_queue_is_visible_and_failed_jobs_can_be_retried(self):
        conn = self.app_conn()
        jid = jobs.enqueue(conn, "noop", {})
        conn.execute("UPDATE jobs SET state='failed', last_error='boom' WHERE id = ?", (jid,))
        conn.commit()
        listed = self.client.get("/api/admin/queue").get_json()["jobs"]
        self.assertTrue(any(j["id"] == jid and j["state"] == "failed" for j in listed))
        self.assertEqual(self.call("post", f"/api/admin/queue/{jid}/retry").status_code, 200)
        self.assertEqual(conn.execute("SELECT state FROM jobs WHERE id = ?", (jid,)).fetchone()[0], "pending")

    def test_platform_settings_round_trip_and_validate(self):
        r = self.call("post", "/api/admin/settings", {"default_ttl_hours": 6})
        self.assertEqual(r.get_json()["settings"]["default_ttl_hours"], 6)
        bad = self.call("post", "/api/admin/settings", {"default_ttl_hours": 0})
        self.assertEqual(bad.status_code, 400)
        bad2 = self.call("post", "/api/admin/settings", {"default_ttl_hours": "lots"})
        self.assertEqual(bad2.status_code, 400)

    def test_new_projects_inherit_the_platform_ttl(self):
        self.call("post", "/api/admin/settings", {"default_ttl_hours": 3})
        self.post("/api/auth/logout")
        self.upgrade_student()
        self.as_student()
        proj = self.create(name="Second", deploy=False).get_json()["project"]
        self.assertEqual(proj["ttl_hours"], 3)


class AuthorizationTests(PlatformCase):
    def test_students_cannot_reach_admin_endpoints(self):
        self.as_student()
        for path in ("/api/admin/overview", "/api/admin/deployments", "/api/admin/projects",
                     "/api/admin/queue", "/api/admin/settings", "/api/admin/users"):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertEqual(self.call("post", "/api/admin/settings", {"default_ttl_hours": 1}).status_code, 403)

    def test_anonymous_cannot_reach_admin_endpoints(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/admin/overview").status_code, 401)

    def test_students_cannot_raise_their_own_build_limits(self):
        self.as_student()
        self.create(deploy=False)
        r = self.call("patch", "/api/projects/web-app", {"limits_memory": "64G"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("administrator", r.get_json()["error"])

    def test_admin_can_set_build_limits(self):
        self.as_student()
        self.create(deploy=False)
        self.post("/api/auth/logout")
        self.login(ADMIN[0], ADMIN[2])
        r = self.call("patch", "/api/projects/web-app", {"limits_memory": "1G", "limits_cpus": "1.5"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["project"]["limits_memory"], "1G")
        bad = self.call("patch", "/api/projects/web-app", {"limits_memory": "huge"})
        self.assertEqual(bad.status_code, 400)

    def test_students_cannot_expire_other_peoples_deployments(self):
        self.login(ADMIN[0], ADMIN[2])
        dep_id = self.wait(self.create().get_json()["deployment_id"])["id"]
        self.post("/api/auth/logout")
        self.as_student()
        self.assertEqual(self.call("post", f"/api/admin/deployments/{dep_id}/expire").status_code, 403)
        self.assertEqual(self.client.get(f"/api/deployments/{dep_id}").status_code, 404)

    def test_port_and_ttl_inputs_are_validated(self):
        self.as_student()
        self.assertEqual(self.create(port="  ; drop table").status_code, 400)
        self.assertEqual(self.create(port=99999).status_code, 400)


class RateLimitTests(AppTestCase):
    def test_token_bucket_allows_then_blocks_then_refills(self):
        conn = connect(self.app.config["DATABASE"])
        self.addCleanup(conn.close)
        for _ in range(3):
            self.assertFalse(rate_limit(conn, "b", limit=3, per_seconds=60))
        self.assertTrue(rate_limit(conn, "b", limit=3, per_seconds=60))
        reset(conn, "b")
        self.assertFalse(rate_limit(conn, "b", limit=3, per_seconds=60))

    def test_buckets_are_independent(self):
        conn = connect(self.app.config["DATABASE"])
        self.addCleanup(conn.close)
        for _ in range(3):
            rate_limit(conn, "one", limit=3, per_seconds=60)
        self.assertTrue(rate_limit(conn, "one", limit=3, per_seconds=60))
        self.assertFalse(rate_limit(conn, "two", limit=3, per_seconds=60))

    def test_login_is_rate_limited_by_address(self):
        for _ in range(20):
            self.login("nobody@example.test", "wrong-password-here")
        r = self.login(STUDENT[0], STUDENT[2])
        self.assertEqual(r.status_code, 429)
        self.assertIn("Too many sign-in attempts", r.get_json()["error"])

    def test_rate_limit_survives_a_process_restart(self):
        """The bucket lives in SQLite, so a reload does not hand out a fresh allowance."""
        conn = connect(self.app.config["DATABASE"])
        self.addCleanup(conn.close)
        for _ in range(3):
            rate_limit(conn, "persist", limit=3, per_seconds=600)
        other = connect(self.app.config["DATABASE"])
        self.addCleanup(other.close)
        self.assertTrue(rate_limit(other, "persist", limit=3, per_seconds=600))


class DeployRateLimitTests(PlatformCase):
    def test_deploy_endpoint_is_rate_limited(self):
        self.as_student()
        self.create(deploy=False)
        codes = [self.call("post", "/api/projects/web-app/deploy").status_code for _ in range(32)]
        self.assertIn(429, codes)
