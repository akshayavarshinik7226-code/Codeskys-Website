import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

MIGRATIONS = [
    # 1: users + audit log
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('admin','student')),
        password_hash TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        failed_logins INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT,
        created_at TEXT NOT NULL,
        last_login_at TEXT
    );
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        actor_id INTEGER,
        action TEXT NOT NULL,
        target TEXT,
        ip TEXT,
        detail TEXT
    );
    CREATE INDEX idx_audit_ts ON audit_log(ts);
    """,
    # 2: deployment platform — projects, deployments, logs, env, domains, tokens, notifications, settings
    """
    ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'hobby';
    CREATE TABLE projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        repo_url TEXT NOT NULL,
        repo_full_name TEXT NOT NULL DEFAULT '',
        branch TEXT NOT NULL DEFAULT 'main',
        framework TEXT NOT NULL DEFAULT 'static',
        root_dir TEXT NOT NULL DEFAULT '',
        install_command TEXT NOT NULL DEFAULT '',
        build_command TEXT NOT NULL DEFAULT '',
        output_dir TEXT NOT NULL DEFAULT '',
        start_command TEXT NOT NULL DEFAULT '',
        port INTEGER NOT NULL DEFAULT 3000,
        node_version TEXT NOT NULL DEFAULT '20.x',
        auto_deploy INTEGER NOT NULL DEFAULT 1,
        preview_deploys INTEGER NOT NULL DEFAULT 1,
        is_paused INTEGER NOT NULL DEFAULT 0,
        provider TEXT NOT NULL DEFAULT 'simulator',
        provider_ref TEXT,
        hook_token TEXT NOT NULL,
        current_deployment_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_projects_owner ON projects(owner_id);
    CREATE TABLE deployments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        number INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('queued','building','ready','failed','cancelled')),
        stage TEXT NOT NULL DEFAULT 'queued',
        target TEXT NOT NULL DEFAULT 'production' CHECK (target IN ('production','preview')),
        branch TEXT NOT NULL,
        commit_sha TEXT NOT NULL DEFAULT '',
        commit_message TEXT NOT NULL DEFAULT '',
        trigger TEXT NOT NULL DEFAULT 'manual',
        triggered_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        url TEXT,
        provider TEXT NOT NULL DEFAULT 'simulator',
        provider_ref TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        duration_ms INTEGER
    );
    CREATE INDEX idx_deploy_project ON deployments(project_id, id DESC);
    CREATE TABLE deploy_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deployment_id INTEGER NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
        ts TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'info',
        stage TEXT NOT NULL DEFAULT '',
        line TEXT NOT NULL
    );
    CREATE INDEX idx_logs_deploy ON deploy_logs(deployment_id, id);
    CREATE TABLE env_vars (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        is_secret INTEGER NOT NULL DEFAULT 1,
        targets TEXT NOT NULL DEFAULT 'production,preview',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(project_id, key)
    );
    CREATE TABLE domains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        hostname TEXT NOT NULL UNIQUE COLLATE NOCASE,
        is_primary INTEGER NOT NULL DEFAULT 0,
        is_system INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        verify_token TEXT NOT NULL,
        https INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        verified_at TEXT
    );
    CREATE TABLE api_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        prefix TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        expires_at TEXT
    );
    CREATE TABLE notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        link TEXT,
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_notif_user ON notifications(user_id, id DESC);
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    ALTER TABLE audit_log ADD COLUMN project_id INTEGER;
    CREATE INDEX idx_audit_project ON audit_log(project_id, id DESC);
    """,
    # 3: git providers, durable job queue, webhooks, rate limits, deployment expiry
    """
    CREATE TABLE git_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider TEXT NOT NULL CHECK (provider IN ('github','gitlab')),
        external_id TEXT NOT NULL,
        username TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        host TEXT NOT NULL DEFAULT '',
        scopes TEXT NOT NULL DEFAULT '',
        token_cipher TEXT NOT NULL,
        refresh_cipher TEXT,
        token_expires_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(user_id, provider)
    );

    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        dedupe_key TEXT UNIQUE,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending','running','done','failed','cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        run_after TEXT NOT NULL,
        locked_by TEXT,
        locked_until TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_jobs_ready ON jobs(state, run_after);

    CREATE TABLE webhook_deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        delivery_id TEXT NOT NULL,
        event TEXT NOT NULL DEFAULT '',
        commit_sha TEXT NOT NULL DEFAULT '',
        result TEXT NOT NULL DEFAULT '',
        received_at TEXT NOT NULL,
        UNIQUE(provider, delivery_id)
    );

    CREATE TABLE rate_limits (
        bucket TEXT PRIMARY KEY,
        tokens REAL NOT NULL,
        updated_at TEXT NOT NULL
    );

    ALTER TABLE projects ADD COLUMN git_account_id INTEGER REFERENCES git_accounts(id) ON DELETE SET NULL;
    ALTER TABLE projects ADD COLUMN git_provider TEXT NOT NULL DEFAULT 'github';
    ALTER TABLE projects ADD COLUMN repo_external_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE projects ADD COLUMN is_private INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE projects ADD COLUMN webhook_secret TEXT NOT NULL DEFAULT '';
    ALTER TABLE projects ADD COLUMN ttl_hours INTEGER NOT NULL DEFAULT 24;
    ALTER TABLE projects ADD COLUMN limits_memory TEXT NOT NULL DEFAULT '512M';
    ALTER TABLE projects ADD COLUMN limits_cpus TEXT NOT NULL DEFAULT '0.5';
    ALTER TABLE projects ADD COLUMN coolify_app_name TEXT NOT NULL DEFAULT '';

    -- deployments gains 'expired' as a terminal state, so the CHECK is rebuilt.
    CREATE TABLE deployments_v3 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        number INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('queued','building','ready','failed','cancelled','expired')),
        stage TEXT NOT NULL DEFAULT 'queued',
        target TEXT NOT NULL DEFAULT 'production' CHECK (target IN ('production','preview')),
        branch TEXT NOT NULL,
        commit_sha TEXT NOT NULL DEFAULT '',
        commit_message TEXT NOT NULL DEFAULT '',
        trigger TEXT NOT NULL DEFAULT 'manual',
        triggered_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
        url TEXT,
        provider TEXT NOT NULL DEFAULT 'simulator',
        provider_ref TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        duration_ms INTEGER,
        expires_at TEXT,
        expired_at TEXT,
        teardown_note TEXT
    );
    INSERT INTO deployments_v3 (id, project_id, number, status, stage, target, branch, commit_sha,
        commit_message, trigger, triggered_by, url, provider, provider_ref, error, created_at,
        started_at, finished_at, duration_ms)
      SELECT id, project_id, number, status, stage, target, branch, commit_sha, commit_message,
             trigger, triggered_by, url, provider, provider_ref, error, created_at, started_at,
             finished_at, duration_ms FROM deployments;
    DROP TABLE deployments;
    ALTER TABLE deployments_v3 RENAME TO deployments;
    CREATE INDEX idx_deploy_project ON deployments(project_id, id DESC);
    CREATE INDEX idx_deploy_expiry ON deployments(status, expires_at);
    """,
    # 4: project forms and encrypted environment variable values
    """
    ALTER TABLE env_vars ADD COLUMN value_cipher TEXT;

    CREATE TABLE project_forms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        slug TEXT NOT NULL,
        public_key TEXT NOT NULL UNIQUE,
        fields_json TEXT NOT NULL DEFAULT '[]',
        success_message TEXT NOT NULL DEFAULT 'Thanks — your response has been received.',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(project_id, slug)
    );
    CREATE INDEX idx_forms_project ON project_forms(project_id, id DESC);

    CREATE TABLE form_submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        form_id INTEGER NOT NULL REFERENCES project_forms(id) ON DELETE CASCADE,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL,
        ip_hash TEXT NOT NULL DEFAULT '',
        user_agent TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_form_submissions_form ON form_submissions(form_id, id DESC);
    """,
    # 5: Codeskys authorization (authz) linkage and live-application state.
    #    authz_project_id  -> the ForgeOps authz project (prj_...) this project deploys as
    #    github_repo_id    -> GitHub's numeric id, as VERIFIED by authz (never user input)
    #    github_installation_id -> codeskys-deploy App installation for private repos
    #    deployments.authz_deployment_id -> the deployment-bound credential (dep_...)
    """
    ALTER TABLE projects ADD COLUMN authz_project_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE projects ADD COLUMN github_repo_id INTEGER;
    ALTER TABLE projects ADD COLUMN github_installation_id INTEGER;
    ALTER TABLE projects ADD COLUMN app_state TEXT NOT NULL DEFAULT '';
    ALTER TABLE deployments ADD COLUMN authz_deployment_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE deployments ADD COLUMN authz_revoked_at TEXT;
    """,
    # 6: billing (Razorpay). Additive only. users.plan_expires_at NULL = never expires
    #    (administrator-set plans keep working exactly as before).
    #    payments.status: created | pending | paid | failed | refunded | partially_refunded
    """
    ALTER TABLE users ADD COLUMN plan_expires_at TEXT;
    CREATE TABLE payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ref TEXT NOT NULL UNIQUE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        plan TEXT NOT NULL,
        grants_plan TEXT NOT NULL,
        period_days INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        currency TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'created',
        razorpay_order_id TEXT NOT NULL UNIQUE,
        razorpay_payment_id TEXT NOT NULL DEFAULT '',
        refunded_amount INTEGER NOT NULL DEFAULT 0,
        entitlement_granted INTEGER NOT NULL DEFAULT 0,
        entitlement_until TEXT,
        failure_reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        paid_at TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_payments_user ON payments(user_id, id DESC);
    CREATE TABLE payment_refunds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE RESTRICT,
        razorpay_refund_id TEXT NOT NULL UNIQUE,
        amount INTEGER NOT NULL,
        status TEXT NOT NULL,
        requested_by INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE payment_events (
        event_id TEXT PRIMARY KEY,
        event TEXT NOT NULL,
        received_at TEXT NOT NULL
    );
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def migrate(path: str) -> int:
    """Apply pending migrations. Returns the resulting schema version.

    Foreign keys and the modern ALTER TABLE rewrite are both disabled while a
    migration runs: migration 3 rebuilds ``deployments`` to widen a CHECK
    constraint, and with foreign keys on, dropping the old table would cascade
    away every row in ``deploy_logs``.
    """
    conn = connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] or 0
        for number, script in enumerate(MIGRATIONS, start=1):
            if number > current:
                conn.executescript(script)
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
                conn.commit()
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:  # pragma: no cover - would mean a bad migration shipped
            raise RuntimeError(f"Migration left dangling references: {[tuple(b) for b in broken[:5]]}")
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
        return len(MIGRATIONS)
    finally:
        conn.close()


def audit(action: str, actor_id=None, target=None, ip=None, detail=None, project_id=None):
    db = get_db()
    db.execute(
        "INSERT INTO audit_log (ts, actor_id, action, target, ip, detail, project_id) VALUES (?,?,?,?,?,?,?)",
        (utcnow(), actor_id, action, target, ip, detail, project_id),
    )
    db.commit()


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utcnow()),
    )
    conn.commit()
