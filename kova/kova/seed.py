"""Creates a realistic demo workspace so the dashboard can be explored immediately."""
import random
import secrets
from datetime import datetime, timedelta, timezone

from .db import connect, utcnow
from .gitinfo import FRAMEWORKS

SAMPLES = [
    ("Portfolio Site", "portfolio-site", "nextjs", "https://github.com/vercel/nextjs-portfolio-starter", "Personal portfolio and blog"),
    ("Campus Connect", "campus-connect", "react", "https://github.com/vitejs/vite-plugin-react", "Student community dashboard"),
    ("Weather Board", "weather-board", "vue", "https://github.com/coollabsio/coolify-examples", "Live weather visualisation"),
    ("Orders API", "orders-api", "node", "https://github.com/heroku/node-js-getting-started", "REST API for the storefront"),
    ("Analytics Service", "analytics-service", "flask", "https://github.com/heroku/python-getting-started", "Python reporting service"),
    ("Docs Site", "docs-site", "static", "https://github.com/cloudacademy/static-website-example", "Product documentation"),
]
MESSAGES = ["Fix mobile navigation overflow", "Add dark mode toggle", "Update dependencies", "Improve build caching",
            "Refactor API client", "Add error boundary", "Tune lighthouse score", "Fix failing unit test",
            "Add pagination to results", "Polish empty states"]
LOGS = {
    "initializing": ["Build machine: 2 vCPU · 4 GB · region bom1 (Mumbai)", "Provider: Codeskys simulator"],
    "cloning": ["Cloning repository (branch: main)", "Checked out {sha} — {msg}", "Detected framework: {fw}"],
    "installing": ["Restoring build cache…", "Running \"{install}\"", "added {n} packages in {t}s", "found 0 vulnerabilities"],
    "building": ["Running \"{build}\"", "  dist/assets/index-{hex}.js   142.8 kB │ gzip: 46.1 kB",
                 "  dist/assets/index-{hex}.css   18.2 kB │ gzip:  4.3 kB", "✓ built in {t}s"],
    "deploying": ["Uploading build output", "Edge cache invalidated across 14 regions", "Deployment ready"],
}


def seed(db_path: str, owner_id: int, base_domain="codeskys.com"):
    conn = connect(db_path)
    rnd = random.Random(7)
    now = datetime.now(timezone.utc)
    created = 0
    try:
        for idx, (name, slug, fw_id, repo, desc) in enumerate(SAMPLES):
            if conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
                continue
            fw = FRAMEWORKS[fw_id]
            made = (now - timedelta(days=rnd.randint(20, 90))).isoformat(timespec="seconds")
            full = "/".join(repo.rstrip("/").split("/")[-2:])
            cur = conn.execute(
                "INSERT INTO projects (owner_id, name, slug, description, repo_url, repo_full_name, branch, framework, "
                "install_command, build_command, output_dir, start_command, port, hook_token, created_at, updated_at, "
                "webhook_secret, ttl_hours, git_provider) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner_id, name, slug, desc, repo, full, "main", fw_id, fw["install"], fw["build"], fw["output"],
                 fw["start"], fw["port"], secrets.token_urlsafe(24), made, made,
                 secrets.token_urlsafe(32), 24, "github"))
            pid = cur.lastrowid
            conn.execute("INSERT INTO domains (project_id, hostname, is_primary, is_system, status, verify_token, "
                         "created_at, verified_at) VALUES (?,?,1,1,'active',?,?,?)",
                         (pid, f"{slug}.{base_domain}", secrets.token_hex(8), made, made))
            for key, val in (("NODE_ENV", "production"), ("API_BASE_URL", "https://api.example.com")):
                conn.execute("INSERT INTO env_vars (project_id, key, value, is_secret, targets, created_at, updated_at) "
                             "VALUES (?,?,?,?,?,?,?)", (pid, key, val, 0,
                                                        "production,preview", made, made))
            count = rnd.randint(4, 9)
            last_ready = None
            for n in range(1, count + 1):
                age = timedelta(days=(count - n) * rnd.uniform(0.6, 3.4), hours=rnd.uniform(0, 12))
                at = now - age
                failed = (n not in (count,)) and rnd.random() < 0.18
                preview = rnd.random() < 0.25 and n != count
                dur = rnd.randint(18000, 96000)
                status = "failed" if failed else "ready"
                finished = at + timedelta(milliseconds=dur)
                sha = secrets.token_hex(20)
                msg = MESSAGES[(idx * 3 + n) % len(MESSAGES)]
                url = f"/sites/{slug}/" if status == "ready" else None
                # Live production builds carry a TTL so the expiry screens have real data.
                expires = None
                if status == "ready" and not preview and n == count:
                    expires = (finished + timedelta(hours=24)).isoformat(timespec="seconds")
                dcur = conn.execute(
                    "INSERT INTO deployments (project_id, number, status, stage, target, branch, commit_sha, "
                    "commit_message, trigger, triggered_by, url, provider, error, created_at, started_at, finished_at, "
                    "duration_ms, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, n, status, status, "preview" if preview else "production",
                     "feature/ui" if preview else "main", sha, msg,
                     rnd.choice(["manual", "hook", "initial" if n == 1 else "manual"]), owner_id, url, "simulator",
                     "Build command exited with code 1." if failed else None,
                     at.isoformat(timespec="seconds"), at.isoformat(timespec="seconds"),
                     finished.isoformat(timespec="seconds"), dur, expires))
                did = dcur.lastrowid
                if status == "ready" and not preview:
                    last_ready = did
                ts = at
                for stage, lines in LOGS.items():
                    if stage == "installing" and not fw["install"]:
                        continue
                    if stage == "building" and not fw["build"]:
                        continue
                    for line in lines:
                        ts += timedelta(seconds=rnd.uniform(0.4, 3.0))
                        text = (line.replace("{sha}", sha[:7]).replace("{msg}", msg).replace("{fw}", fw["name"])
                                .replace("{install}", fw["install"]).replace("{build}", fw["build"])
                                .replace("{n}", str(rnd.randint(180, 1200))).replace("{t}", f"{rnd.uniform(2, 15):.2f}")
                                .replace("{hex}", secrets.token_hex(4)))
                        level = "success" if text.startswith("✓") or "ready" in text else "info"
                        conn.execute("INSERT INTO deploy_logs (deployment_id, ts, level, stage, line) VALUES (?,?,?,?,?)",
                                     (did, ts.isoformat(timespec="seconds"), level, stage, text))
                    if failed and stage == "building":
                        conn.execute("INSERT INTO deploy_logs (deployment_id, ts, level, stage, line) VALUES (?,?,?,?,?)",
                                     (did, ts.isoformat(timespec="seconds"), "error", "building",
                                      "Error: Command failed with exit code 1"))
                        break
                conn.execute("INSERT INTO audit_log (ts, actor_id, action, target, detail, project_id) VALUES (?,?,?,?,?,?)",
                             (at.isoformat(timespec="seconds"), owner_id, "deploy.triggered", f"#{n}", None, pid))
            conn.execute("UPDATE projects SET current_deployment_id = ?, updated_at = ? WHERE id = ?",
                         (last_ready, (now - timedelta(hours=rnd.randint(1, 60))).isoformat(timespec="seconds"), pid))
            conn.execute("INSERT INTO audit_log (ts, actor_id, action, target, project_id) VALUES (?,?,?,?,?)",
                         (made, owner_id, "project.created", slug, pid))
            created += 1
        conn.execute("INSERT INTO notifications (user_id, kind, title, body, link, is_read, created_at) VALUES (?,?,?,?,?,?,?)",
                     (owner_id, "deploy.ready", "Portfolio Site is live", "Production deployment published.",
                      "#/projects/portfolio-site", 0, utcnow()))
        conn.commit()
        return created
    finally:
        conn.close()
