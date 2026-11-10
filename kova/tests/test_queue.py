"""Durable queue, scheduler and expiry behaviour."""
import json
import time
from datetime import datetime, timedelta, timezone

from kova import engine, jobs
from kova.db import connect, utcnow

from .base import STUDENT, AppTestCase
from .test_platform import PlatformCase


def iso(dt):
    return dt.isoformat(timespec="seconds")


class QueueMechanicsTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.conn = connect(self.app.config["DATABASE"])
        self.addCleanup(self.conn.close)

    def test_enqueue_and_claim(self):
        jid = jobs.enqueue(self.conn, "noop", {"a": 1})
        self.assertTrue(jid)
        job = jobs.claim(self.conn, "worker-a")
        self.assertEqual(job["id"], jid)
        self.assertEqual(job["state"], "running")
        self.assertEqual(job["locked_by"], "worker-a")
        self.assertEqual(json.loads(job["payload"]), {"a": 1})

    def test_dedupe_key_blocks_a_second_copy(self):
        self.assertTrue(jobs.enqueue(self.conn, "deploy", {"deployment_id": 1}, dedupe_key="deploy:1"))
        self.assertIsNone(jobs.enqueue(self.conn, "deploy", {"deployment_id": 1}, dedupe_key="deploy:1"))
        n = self.conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_two_workers_never_hold_the_same_job(self):
        jobs.enqueue(self.conn, "noop", {})
        other = connect(self.app.config["DATABASE"])
        self.addCleanup(other.close)
        first = jobs.claim(self.conn, "worker-a")
        second = jobs.claim(other, "worker-b")
        self.assertIsNotNone(first)
        self.assertIsNone(second, "a leased job must not be claimable by a second worker")

    def test_expired_lease_is_reclaimed_after_a_crash(self):
        jid = jobs.enqueue(self.conn, "noop", {})
        jobs.claim(self.conn, "worker-that-died", lease=1)
        self.conn.execute("UPDATE jobs SET locked_until = ? WHERE id = ?",
                          (iso(datetime.now(timezone.utc) - timedelta(seconds=5)), jid))
        self.conn.commit()
        again = jobs.claim(self.conn, "worker-b")
        self.assertEqual(again["id"], jid)
        self.assertEqual(again["locked_by"], "worker-b")
        self.assertEqual(again["attempts"], 2)

    def test_future_jobs_are_not_claimed_early(self):
        jobs.enqueue(self.conn, "noop", {}, delay=600)
        self.assertIsNone(jobs.claim(self.conn, "worker-a"))

    def test_failure_retries_with_backoff_then_gives_up(self):
        calls = []

        @jobs.handler("always-fails")
        def boom(conn, config, payload, beat):
            calls.append(1)
            raise RuntimeError("nope")

        jobs.enqueue(self.conn, "always-fails", {}, max_attempts=2)
        job = jobs.claim(self.conn, "w")
        jobs.run_one(self.app.config["DATABASE"], self.app.config, job, "w", conn=self.conn)
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        self.assertEqual(row["state"], "pending")          # retrying
        self.assertIn("nope", row["last_error"])
        self.assertGreater(row["run_after"], utcnow())      # backed off

        self.conn.execute("UPDATE jobs SET run_after = ? WHERE id = ?", (utcnow(), job["id"]))
        self.conn.commit()
        job2 = jobs.claim(self.conn, "w")
        jobs.run_one(self.app.config["DATABASE"], self.app.config, job2, "w", conn=self.conn)
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(len(calls), 2)
        jobs.HANDLERS.pop("always-fails", None)

    def test_heartbeat_extends_lease_and_detects_steal(self):
        jid = jobs.enqueue(self.conn, "noop", {})
        jobs.claim(self.conn, "worker-a", lease=60)
        self.assertTrue(jobs.heartbeat(self.conn, jid, "worker-a"))
        self.assertFalse(jobs.heartbeat(self.conn, jid, "worker-b"))


class DeploymentRecoveryTests(PlatformCase):
    def test_deploy_is_queued_not_run_inline(self):
        self.as_student()
        dep_id = self.create().get_json()["deployment_id"]
        row = self.app_conn().execute("SELECT * FROM jobs WHERE dedupe_key = ?", (f"deploy:{dep_id}",)).fetchone()
        self.assertIsNotNone(row, "creating a project must enqueue a durable job")
        self.assertEqual(row["state"], "pending")
        d = self.client.get(f"/api/deployments/{dep_id}").get_json()["deployment"]
        self.assertEqual(d["status"], "queued")

    def test_worker_restart_resumes_a_queued_build(self):
        self.as_student()
        dep_id = self.create().get_json()["deployment_id"]
        # Nothing ran yet — exactly the state after a crash before the first tick.
        self.work()
        d = self.client.get(f"/api/deployments/{dep_id}").get_json()["deployment"]
        self.assertEqual(d["status"], "ready")

    def test_sweep_rescues_a_build_whose_job_vanished(self):
        self.as_student()
        dep_id = self.create().get_json()["deployment_id"]
        conn = self.app_conn()
        conn.execute("DELETE FROM jobs WHERE dedupe_key = ?", (f"deploy:{dep_id}",))
        conn.execute("UPDATE deployments SET status='building', stage='building' WHERE id = ?", (dep_id,))
        conn.commit()
        jobs.sweep(conn, self.app.config)
        self.assertIsNotNone(conn.execute("SELECT 1 FROM jobs WHERE dedupe_key = ?",
                                          (f"deploy:{dep_id}",)).fetchone())
        self.work()
        d = self.client.get(f"/api/deployments/{dep_id}").get_json()["deployment"]
        self.assertEqual(d["status"], "ready")

    def test_sweep_fails_a_build_whose_job_already_finished(self):
        self.as_student()
        dep_id = self.create().get_json()["deployment_id"]
        conn = self.app_conn()
        conn.execute("UPDATE jobs SET state='done' WHERE dedupe_key = ?", (f"deploy:{dep_id}",))
        conn.execute("UPDATE deployments SET status='building' WHERE id = ?", (dep_id,))
        conn.commit()
        jobs.sweep(conn, self.app.config)
        d = self.client.get(f"/api/deployments/{dep_id}").get_json()["deployment"]
        self.assertEqual(d["status"], "failed")
        self.assertIn("worker stopped", d["error"])

    def test_giving_up_marks_the_deployment_failed(self):
        self.as_student()
        dep_id = self.create(deploy=False).get_json()["project"]["id"] and None
        conn = self.app_conn()
        # Point a deploy job at a deployment id that cannot load, and exhaust it.
        proj = conn.execute("SELECT * FROM projects LIMIT 1").fetchone()
        conn.execute("INSERT INTO deployments (project_id, number, status, stage, target, branch, created_at) "
                     "VALUES (?,99,'queued','queued','production','main',?)", (proj["id"], utcnow()))
        conn.commit()
        bad_id = conn.execute("SELECT MAX(id) AS i FROM deployments").fetchone()["i"]
        jobs.enqueue(conn, "deploy", {"deployment_id": bad_id}, dedupe_key=f"deploy:{bad_id}", max_attempts=1)

        @jobs.handler("deploy")
        def explode(conn_, config, payload, beat):
            raise RuntimeError("provider exploded")
        try:
            job = jobs.claim(conn, "w")
            jobs.run_one(self.app.config["DATABASE"], self.app.config, job, "w", conn=conn)
        finally:
            jobs.HANDLERS["deploy"] = engine.run_deployment
        row = conn.execute("SELECT * FROM deployments WHERE id = ?", (bad_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("gave up", row["error"])


class ExpiryTests(PlatformCase):
    def test_ready_deployment_gets_a_24h_expiry(self):
        self.as_student()
        d = self.wait(self.create().get_json()["deployment_id"])
        self.assertIsNotNone(d["expires_at"])
        created = datetime.fromisoformat(d["finished_at"])
        expires = datetime.fromisoformat(d["expires_at"])
        self.assertAlmostEqual((expires - created).total_seconds(), 24 * 3600, delta=120)

    def test_sweep_expires_a_deployment_past_its_ttl(self):
        self.as_student()
        d = self.wait(self.create().get_json()["deployment_id"])
        conn = self.app_conn()
        conn.execute("UPDATE deployments SET expires_at = ? WHERE id = ?",
                     (iso(datetime.now(timezone.utc) - timedelta(minutes=1)), d["id"]))
        conn.commit()
        self.assertGreaterEqual(jobs.sweep(conn, self.app.config), 1)
        self.work()
        after = self.client.get(f"/api/deployments/{d['id']}").get_json()["deployment"]
        self.assertEqual(after["status"], "expired")
        self.assertIsNotNone(after["expired_at"])
        proj = self.client.get("/api/projects/web-app").get_json()["project"]
        self.assertIsNone(proj["production"], "an expired deployment must stop being production")

    def test_expiry_is_idempotent(self):
        self.as_student()
        d = self.wait(self.create().get_json()["deployment_id"])
        conn = self.app_conn()
        conn.execute("UPDATE deployments SET expires_at = ? WHERE id = ?",
                     (iso(datetime.now(timezone.utc) - timedelta(minutes=1)), d["id"]))
        conn.commit()
        engine.expire_deployment(conn, self.app.config, {"deployment_id": d["id"]}, lambda *a, **k: True)
        first = conn.execute("SELECT expired_at FROM deployments WHERE id = ?", (d["id"],)).fetchone()["expired_at"]
        engine.expire_deployment(conn, self.app.config, {"deployment_id": d["id"]}, lambda *a, **k: True)
        again = conn.execute("SELECT expired_at FROM deployments WHERE id = ?", (d["id"],)).fetchone()["expired_at"]
        self.assertEqual(first, again)

    def test_expired_site_stops_serving(self):
        self.as_student()
        d = self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(self.client.get("/sites/web-app/").status_code, 200)
        conn = self.app_conn()
        conn.execute("UPDATE deployments SET expires_at = ? WHERE id = ?",
                     (iso(datetime.now(timezone.utc) - timedelta(minutes=1)), d["id"]))
        conn.commit()
        jobs.sweep(conn, self.app.config)
        self.work()
        self.assertEqual(self.client.get("/sites/web-app/").status_code, 404)

    def test_custom_ttl_is_honoured(self):
        self.as_student()
        r = self.create(ttl_hours=2)
        d = self.wait(r.get_json()["deployment_id"])
        delta = datetime.fromisoformat(d["expires_at"]) - datetime.fromisoformat(d["finished_at"])
        self.assertAlmostEqual(delta.total_seconds(), 2 * 3600, delta=120)

    def test_ttl_is_validated(self):
        self.as_student()
        self.assertEqual(self.create(ttl_hours=0).status_code, 400)
        self.assertEqual(self.create(ttl_hours="soon").status_code, 400)
