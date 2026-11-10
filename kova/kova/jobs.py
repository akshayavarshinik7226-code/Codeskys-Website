"""Database-backed job queue, worker and scheduler.

Deployments used to run on a bare ``threading.Thread``: if the process stopped
mid-build the deployment was stranded in ``building`` forever and nothing retried
it. Work is now a row in ``jobs``, claimed under a lease:

* **Atomic claim** — a worker takes a job inside ``BEGIN IMMEDIATE``, so two
  workers can never hold the same job.
* **Leases** — a claim expires (``locked_until``). A worker that dies has its job
  picked up by the next worker instead of blocking the queue forever.
* **Retries** — failures are retried with exponential backoff up to
  ``max_attempts``, then the job is marked failed and the deployment with it.
* **Restart recovery** — nothing lives in memory, so a restart resumes work.

The same loop runs the scheduler: every tick it sweeps deployments whose TTL has
passed and enqueues their teardown.
"""
import json
import os
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from .db import connect, utcnow

HANDLERS = {}
DEFAULT_LEASE = 900          # 15 min; long builds heartbeat to extend it
SWEEP_EVERY = 30             # seconds between expiry sweeps
BACKOFF = (15, 60, 300)      # retry delays per attempt


def handler(kind):
    def register(fn):
        HANDLERS[kind] = fn
        return fn
    return register


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _now():
    return datetime.now(timezone.utc)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


# ---------------------------------------------------------------- producing

def enqueue(conn, kind, payload=None, *, dedupe_key=None, delay=0, max_attempts=3):
    """Add a job. Returns its id, or None when ``dedupe_key`` is already queued."""
    now = _now()
    row = (kind, json.dumps(payload or {}), dedupe_key, _iso(now + timedelta(seconds=delay)),
           max_attempts, _iso(now), _iso(now))
    try:
        cur = conn.execute(
            "INSERT INTO jobs (kind, payload, dedupe_key, run_after, max_attempts, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", row)
        conn.commit()
        return cur.lastrowid
    except Exception as exc:  # sqlite3.IntegrityError on the unique dedupe_key
        if "UNIQUE" not in str(exc):
            raise
        conn.rollback()
        return None


# ---------------------------------------------------------------- consuming

def claim(conn, who, lease=DEFAULT_LEASE):
    """Atomically take the next due job. Also reclaims jobs with an expired lease."""
    now = _now()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM jobs WHERE run_after <= ? AND ("
            "  state = 'pending' OR (state = 'running' AND (locked_until IS NULL OR locked_until < ?))"
            ") ORDER BY id LIMIT 1", (_iso(now), _iso(now))).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE jobs SET state='running', attempts = attempts + 1, locked_by = ?, "
            "locked_until = ?, updated_at = ? WHERE id = ?",
            (who, _iso(now + timedelta(seconds=lease)), _iso(now), row["id"]))
        conn.execute("COMMIT")
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # pragma: no cover
            pass
        raise
    finally:
        conn.isolation_level = ""


def heartbeat(conn, job_id, who, lease=DEFAULT_LEASE):
    """Extend a lease mid-build. Returns False if the job was taken away."""
    cur = conn.execute(
        "UPDATE jobs SET locked_until = ?, updated_at = ? WHERE id = ? AND locked_by = ?",
        (_iso(_now() + timedelta(seconds=lease)), utcnow(), job_id, who))
    conn.commit()
    return cur.rowcount > 0


def complete(conn, job_id):
    conn.execute("UPDATE jobs SET state='done', locked_by=NULL, locked_until=NULL, updated_at=? "
                 "WHERE id = ?", (utcnow(), job_id))
    conn.commit()


def fail(conn, job, error):
    """Retry with backoff, or give up once max_attempts is reached."""
    attempts = job["attempts"]
    error = (error or "")[:1000]
    if attempts >= job["max_attempts"]:
        conn.execute("UPDATE jobs SET state='failed', last_error=?, locked_by=NULL, locked_until=NULL, "
                     "updated_at=? WHERE id = ?", (error, utcnow(), job["id"]))
        conn.commit()
        return False
    delay = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
    conn.execute("UPDATE jobs SET state='pending', last_error=?, locked_by=NULL, locked_until=NULL, "
                 "run_after=?, updated_at=? WHERE id = ?",
                 (error, _iso(_now() + timedelta(seconds=delay)), utcnow(), job["id"]))
    conn.commit()
    return True


def run_one(db_path, app_config, job, who, conn=None):
    """Execute a claimed job. Returns True when it finished for good."""
    own = conn is None
    conn = conn or connect(db_path)
    try:
        fn = HANDLERS.get(job["kind"])
        if fn is None:
            fail(conn, job, f"No handler registered for job kind '{job['kind']}'.")
            return False
        beat = lambda lease=DEFAULT_LEASE: heartbeat(conn, job["id"], who, lease)
        try:
            fn(conn, app_config, json.loads(job["payload"] or "{}"), beat)
        except Exception as exc:
            detail = f"{exc.__class__.__name__}: {exc}"
            retrying = fail(conn, job, detail)
            if not retrying:
                _give_up(conn, job, detail)
            return not retrying
        complete(conn, job["id"])
        return True
    finally:
        if own:
            conn.close()


def _give_up(conn, job, detail):
    """A job out of retries must not leave its deployment stuck in 'building'."""
    try:
        payload = json.loads(job["payload"] or "{}")
    except ValueError:  # pragma: no cover
        return
    dep_id = payload.get("deployment_id")
    if job["kind"] == "deploy" and dep_id:
        conn.execute(
            "UPDATE deployments SET status='failed', stage='failed', error=?, finished_at=? "
            "WHERE id = ? AND status IN ('queued','building')",
            (f"Build gave up after {job['attempts']} attempts. {detail}"[:500], utcnow(), dep_id))
        conn.commit()


# ---------------------------------------------------------------- scheduler

def sweep(conn, app_config):
    """Enqueue expiry for deployments past their TTL and rescue orphaned builds."""
    now = utcnow()
    due = conn.execute(
        "SELECT id FROM deployments WHERE status = 'ready' AND expired_at IS NULL "
        "AND expires_at IS NOT NULL AND expires_at <= ? LIMIT 200", (now,)).fetchall()
    queued = 0
    for row in due:
        if enqueue(conn, "expire", {"deployment_id": row["id"]}, dedupe_key=f"expire:{row['id']}"):
            queued += 1

    from .ratelimit import prune
    prune(conn)

    # A build whose job vanished (killed mid-write, or a job that was purged) must not
    # sit in 'building' forever.
    stuck = conn.execute(
        "SELECT d.id, d.created_at FROM deployments d WHERE d.status IN ('queued','building') "
        "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.dedupe_key = 'deploy:' || d.id "
        "AND j.state IN ('pending','running'))").fetchall()
    for row in stuck:
        finished = conn.execute(
            "SELECT 1 FROM jobs WHERE dedupe_key = ? AND state IN ('done','failed')",
            (f"deploy:{row['id']}",)).fetchone()
        if finished:
            conn.execute(
                "UPDATE deployments SET status='failed', stage='failed', finished_at=?, "
                "error='The worker stopped before this build finished.' WHERE id = ?",
                (now, row["id"]))
            conn.commit()
        else:
            enqueue(conn, "deploy", {"deployment_id": row["id"]}, dedupe_key=f"deploy:{row['id']}")
            queued += 1
    return queued


# ---------------------------------------------------------------- worker loop

class Worker:
    def __init__(self, db_path, app_config, poll=1.0, lease=DEFAULT_LEASE):
        self.db_path = db_path
        self.app_config = dict(app_config)
        self.poll = poll
        self.lease = lease
        self.id = worker_id()
        self._stop = threading.Event()
        self._last_sweep = 0.0
        self.processed = 0

    def stop(self):
        self._stop.set()

    def tick(self, conn):
        """One pass: sweep if due, then run at most one job. Returns True if work was done."""
        if time.time() - self._last_sweep > SWEEP_EVERY:
            self._last_sweep = time.time()
            try:
                sweep(conn, self.app_config)
            except Exception:  # pragma: no cover - a bad sweep must not kill the worker
                traceback.print_exc()
        job = claim(conn, self.id, self.lease)
        if job is None:
            return False
        run_one(self.db_path, self.app_config, job, self.id, conn=conn)
        self.processed += 1
        return True

    def run(self):
        conn = connect(self.db_path)
        try:
            self._last_sweep = 0.0
            while not self._stop.is_set():
                try:
                    busy = self.tick(conn)
                except Exception:  # pragma: no cover
                    traceback.print_exc()
                    busy = False
                if not busy:
                    self._stop.wait(self.poll)
        finally:
            conn.close()

    def start_thread(self):
        t = threading.Thread(target=self.run, name="kova-worker", daemon=True)
        t.start()
        return t


def drain(db_path, app_config, limit=200, conn=None):
    """Run every ready job to completion. Used by tests and `manage.py work-once`."""
    own = conn is None
    conn = conn or connect(db_path)
    who = worker_id()
    done = 0
    try:
        while done < limit:
            job = claim(conn, who)
            if job is None:
                return done
            run_one(db_path, app_config, job, who, conn=conn)
            done += 1
        return done
    finally:
        if own:
            conn.close()
