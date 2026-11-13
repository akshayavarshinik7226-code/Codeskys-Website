# Codeskys

A student-facing deployment platform. A student connects GitHub or GitLab, picks a repository and a branch, and Codeskys builds it, publishes it at a URL, streams the build log, and tears it down automatically 24 hours later. Students never need to open the Coolify dashboard.

Codeskys runs builds through one of two engines:

| Engine | What it does | When |
| --- | --- | --- |
| **Simulator** (default) | Walks the pipeline and writes logs **without fetching, building or running any repository code**. Every row it creates is marked `simulated` and the UI badges it. | Development and demos, before a server exists. |
| **Coolify** | Creates and drives a real application on your Coolify server over its v1 API: create, configure, deploy, poll status, stream logs, cancel, stop, delete. | Production, once you have a VPS. |

The engine is a setting in **Integrations**. A simulated deployment is never presented as a real one, anywhere.

---

## 1. What is implemented

**Git providers**
- GitHub OAuth and GitLab OAuth (authorization-code flow), with a single-use `state` nonce bound to the server-side session
- Access tokens encrypted at rest with Fernet; GitLab refresh tokens stored and used automatically when the access token expires
- Tokens and OAuth client secrets never reach the browser — not in any API response, not in any page
- Repository browser for both providers, including private repositories, with live branch lists
- Public repositories still work by pasting a URL, with no account connected

**Deployments**
- Durable, database-backed job queue. Jobs are claimed inside `BEGIN IMMEDIATE` under a lease, so two workers can never run the same job
- Retries with exponential backoff (15s / 60s / 300s), then the job and its deployment are marked failed with the reason
- Restart recovery: a worker that dies has its lease expire and the job is picked up again; a Coolify deployment already handed over is *resumed* (polled) rather than triggered twice
- A sweeper rescues builds whose job row vanished, and fails builds whose job finished without completing them
- Cancellation crosses process boundaries through the database, so the API can stop a build running in a separate worker
- Production and preview targets, instant rollback, redeploy, redeploy with cache cleared

**Automatic 24-hour expiry**
- Every successful deployment gets an `expires_at` when it goes live; the lifetime is configurable per project and platform-wide
- The scheduler runs inside the worker loop, so it survives restarts — expiry is computed from the database, never from an in-memory timer
- At expiry Codeskys stops and deletes the Coolify application, marks the deployment `expired`, clears it as production and notifies the owner
- **Only Codeskys-managed applications are ever deleted.** Before deleting, Codeskys checks the remote application's name against the name it gave it; a mismatch refuses the delete and records why
- A superseded (non-live) deployment expires without touching the running application

**Webhooks**
- GitHub: HMAC-SHA256 over the raw body (`X-Hub-Signature-256`), compared in constant time
- GitLab: shared secret in `X-Gitlab-Token`, compared in constant time
- Per-project secrets, so one leaked secret cannot trigger another project
- Duplicate and replayed deliveries are ignored by delivery id; a commit already building is not built again
- Pushes to a non-deploy branch, branch deletions and non-push events are ignored
- An unknown hook id answers exactly like a bad signature, so the endpoint cannot be used to enumerate projects

**Student dashboard** — create project, connect GitHub/GitLab, pick repository and branch, deploy, live URL, deployment history, streaming build logs, status, redeploy, rollback, environment variables (secrets masked, revealed on demand with an audit entry, `.env` bulk import), custom domains with real DNS verification, project settings, lifetime, webhook URLs

**Admin dashboard** — platform overview (students, projects, deploys, failures, build minutes, queue depth, expiring soonest, usage per student), all deployments with filters, all projects with owners and limits, force-expire a live deployment, stop a running build, job queue with retry, platform settings, member management, Coolify connection with a live connection test that lists your servers, projects and deploy keys

**Security** — scrypt password hashes, session rotation on login, CSRF on every mutating session call, five-failure account lockout, database-backed rate limits (login per IP, deploys per user, webhooks per project) that survive a restart, strict CSP, encrypted secrets at rest, server-side ownership checks on every project route, build resource limits that students cannot raise

---

## 2. What still needs external configuration

None of these are code gaps — they need credentials or a server that only you can provide.

| Feature | What is needed | Without it |
| --- | --- | --- |
| GitHub sign-in | An OAuth App; set `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | The Connect button explains it is not configured; public repos still work by URL |
| GitLab sign-in | An OAuth application; set `GITLAB_CLIENT_ID` / `GITLAB_CLIENT_SECRET` | Same |
| Real deployments | A Coolify server: URL, API token, project uuid, server uuid | Builds run on the simulator and are labelled as such |
| Real deployments on Coolify | Coolify 4.7.5 (Services API), a public GitHub repository, linked to Codeskys authorization, and the wildcard base domain | The build fails with a message saying which of these is missing. Coolify's automatic build packs are never used: they build with host networking. Protected Codeskys services (runtime sidecar) are not available to Coolify deployments: Coolify cannot keep the sidecar's network away from the app |
| Private repository deploys | Not available on Coolify yet (the build needs a read-only clone path) | The build is refused with that message |
| Custom project domains | A wildcard DNS record pointing at the Coolify server | The free `<slug>.codeskys.com` subdomain still works |
| Incoming webhooks | A publicly reachable `KOVA_PUBLIC_URL` | Deploy from the dashboard instead |

**Not built at all:** payments/billing enforcement (the billing screen is informational), a general-purpose AI coding-agent runner, a managed per-project database/storage service, a dedicated serverless-functions runtime, a global CDN/edge network, per-project credential issuing, and Slack/email notifications. The feature centre and project workspace label these as requiring an external provider or runtime; they are not presented as active services.

**Project Forms are implemented in this build:** create project-scoped form endpoints, define fields and validation, pause/activate or delete a form, accept rate-limited public submissions, view the latest 500 submissions and export the inbox to CSV. Public submissions are isolated to the owning project. CSV export escapes spreadsheet formula prefixes.

---

## 3. Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `KOVA_ENV` | `development` | `production` requires the two keys below |
| `KOVA_SECRET_KEY` | generated into `instance/` | Session signing. Set a long random value in production |
| `KOVA_ENCRYPTION_KEY` | generated into `instance/` | Encrypts OAuth tokens. **Changing it invalidates every stored token** |
| `KOVA_PUBLIC_URL` | request host | Base URL for OAuth callbacks and webhook URLs |
| `KOVA_HOST` / `KOVA_PORT` | `127.0.0.1` / `5000` | |
| `KOVA_COOKIE_SECURE` | `0` | Set `1` behind HTTPS |
| `KOVA_INSTANCE_DIR` | `./instance` | Database and generated keys |
| `KOVA_SYSTEM_DOMAIN` | `codeskys.com` | Suffix for free project subdomains |
| `KOVA_TTL_HOURS` | `24` | Default deployment lifetime |
| `KOVA_BUILD_MEMORY` | `512M` | Memory ceiling per Coolify application |
| `KOVA_BUILD_CPUS` | `0.5` | CPU ceiling per Coolify application |
| `KOVA_INLINE_WORKER` | `1` | `0` = run `manage.py worker` separately |
| `KOVA_SIM_SPEED` | `1` | Simulator pacing; `0` finishes instantly |
| `KOVA_GIT_LOOKUPS` | `1` | `0` disables outbound Git metadata calls |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | — | GitHub OAuth App |
| `GITLAB_CLIENT_ID` / `GITLAB_CLIENT_SECRET` | — | GitLab application |
| `COOLIFY_URL` / `COOLIFY_TOKEN` | — | Coolify instance and API token |
| `COOLIFY_PROJECT_UUID` / `COOLIFY_SERVER_UUID` | — | Where applications are created |
| `COOLIFY_BASE_DOMAIN` | — | Wildcard domain, e.g. `apps.example.com` |
| `COOLIFY_PRIVATE_KEY_UUID` | — | Deploy key for private repositories |

Coolify values entered in the Integrations screen are stored in the `settings` table; environment variables take priority. The API only ever reports whether a token is set and its last four characters.

---

## Account access: sign up and log in

Codeskys has two account paths on the same welcome screen:

- **Create account** — a new student enters their name, email address and a password of at least 12 characters. The account is created as a student account and is signed in immediately. Public sign-up cannot create an administrator.
- **Log in** — an existing user enters the email address and password already registered for that account.
- The first administrator is created separately by the owner using the `create-admin` command below. Students must never use the admin setup command.

Email-based password reset is not enabled until an email delivery provider is configured. Do not show it as a working feature before that integration is completed.

## 4. Local setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # Windows PowerShell: Copy-Item .env.example .env
python manage.py init-db
python manage.py create-admin --email you@example.com --name "Your Name"
python manage.py demo --email you@example.com    # optional sample data
python run.py
```

Open <http://127.0.0.1:5000>. The admin CLI asks for a password of at least 12 characters.

With `KOVA_INLINE_WORKER=1` (the default) the queue worker runs inside the web process and nothing else is needed. For production:

```bash
KOVA_INLINE_WORKER=0 python run.py       # web
python manage.py worker                   # worker — run one or more
python manage.py worker --once            # drain and exit, for cron
```

Run more than one worker if you want concurrent builds; the lease makes that safe.

### Connecting GitHub locally

GitHub must be able to reach your callback URL. For local work, create an OAuth App with callback `http://127.0.0.1:5000/api/git/github/callback`, set `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` and `KOVA_PUBLIC_URL=http://127.0.0.1:5000`, then use **Settings → Git accounts → Connect**. Incoming webhooks need a public URL, so they only work once Codeskys is reachable from the internet.

---

## 5. Coolify configuration, for when you have the VPS

1. **Install Coolify** on the server (its own installer; free and self-hosted).
2. **DNS** — point a wildcard record at the server: `*.apps.yourdomain.com → <server IP>`. This is what gives each student project its own URL.
3. **Create a project** in Coolify to hold student applications, e.g. "students". Copy its uuid from the URL.
4. **Copy the server uuid** from Coolify → Servers.
5. **Create an API token** under Keys & Tokens with `read`, `write` and `deploy` permissions.
6. **In Codeskys → Integrations → Coolify**, fill in the instance URL and token and press **Test connection**. Codeskys lists your servers, projects and deploy keys; click to select them, then **Save**.
7. Set the base domain to `apps.yourdomain.com`.
8. Switch the engine to **Coolify**. Deploy one project and watch the log.
9. **Private repositories** are refused on Coolify for now (see the table above); the deploy-key and GitHub App settings are kept for when the sidecar build supports them.
10. **For automatic redeploys**: open a project's Settings in Codeskys, copy the GitHub (or GitLab) webhook URL, and add it in the repository's webhook settings. Codeskys shows the URL; the signing secret stays on the server.

Equivalent environment variables, if you prefer configuring by file:

```bash
COOLIFY_URL=https://coolify.yourdomain.com
COOLIFY_TOKEN=...
COOLIFY_PROJECT_UUID=...
COOLIFY_SERVER_UUID=...
COOLIFY_BASE_DOMAIN=apps.yourdomain.com
COOLIFY_PRIVATE_KEY_UUID=...        # only for private repos
```

**Before letting students deploy:** in Coolify, make sure builds do not have the host Docker socket mounted, and leave Codeskys's memory/CPU limits in place. Codeskys itself never executes repository code — it only ever asks Coolify to.

---

## 6. Test results

```bash
python -m unittest discover -s tests -t . -v
python -m compileall -q .
node --check kova/static/js/app.js
```

**130 automated test cases are included.** Run the commands below in an environment with the requirements installed; this ZIP packaging environment did not have Flask installed, so the suite was not independently executed here.

| Module | Tests | Covers |
| --- | --- | --- |
| `test_phase1` | 27 | password hashing, sessions, CSRF, lockout, security headers, static assets, student registration and login |
| `test_platform` | 20 | projects, deployments, ownership, env vars, domains, API tokens, admin members |
| `test_queue` | 18 | claim/lease, dedupe, two-worker exclusion, lease reclaim, retries and give-up, heartbeat, sweep rescue, TTL assignment, expiry, idempotent expiry, expired site stops serving |
| `test_providers` | 24 | encryption round-trip and tamper detection, OAuth config gating, forged `state` rejection, encrypted token storage, token never exposed, repo/branch listing, disconnect, GitHub signature valid/invalid/missing, unknown hook, duplicate delivery, wrong branch, same-commit guard, ping, auto-deploy off, branch deletion, GitLab token valid/invalid/duplicate |
| `test_coolify` | 21 | full lifecycle against a mocked Coolify, POST `/deploy` with GET fallback, failed build reported as failed, API errors surfaced, unreachable server, private repos refused, **build isolation: only the generated compose build pack, non-Dockerfile projects and host-network/insecure/device Dockerfiles refused before a credential exists, other build packs, custom build/start commands, run options and tampered compose files block build and start**, cancellation, teardown stop+delete, **refusal to delete a non-Codeskys application**, superseded deployment left alone, connection test, token never returned, half-configured Coolify falling back to the simulator |
| `test_admin_security` | 20 | admin overview, filters, force expire, stop build, queue retry, settings validation, TTL inheritance, student/anonymous authorization boundaries, students cannot raise build limits, input validation, rate limits per bucket and across restarts, login and deploy throttling |
| `test_forms` | 6 | project-scoped form creation/listing, public submission validation and storage, invalid schema rejection, pause behavior, owner isolation, deletion and submission invalidation |

**What the tests do not prove:** the Coolify tests run against a mocked Coolify server. They verify the request contract — method, path, body, teardown guards, error handling — but they are **not** evidence that a deployment succeeds against a live Coolify instance. That needs a real server and has not been done. The same applies to GitHub/GitLab OAuth: the flows are tested with mocked provider responses, not against real GitHub.

---

## Architecture

```
Browser (vanilla JS, no build step)
  │  session cookie + CSRF, or Authorization: Bearer kova_…
  ▼
Flask application
  ├── auth.py        sign-in, sessions, roles
  ├── providers.py   GitHub/GitLab OAuth, repo and branch browsing
  ├── webhooks.py    signed push webhooks
  ├── projects.py    projects, deployments, env vars, domains, analytics
  ├── account.py     profile, password, API tokens, notifications
  ├── admin.py       members, platform dashboard, Coolify connection
  ├── engine.py      state machine + simulator and Coolify providers
  ├── jobs.py        durable queue, worker, expiry scheduler
  ├── crypto.py      encryption for secrets at rest
  ├── ratelimit.py   database-backed token buckets
  └── db.py          SQLite, forward-only migrations
         │
         ▼
   jobs table ◀── worker process(es) ──▶ Coolify API
```

See `docs/ARCHITECTURE.md` for the data model, the state machine and the scaling notes.

### Known limits

- SQLite suits one machine. Move to Postgres before running multiple web processes under real load.
- The queue polls once a second. Fine for a classroom; use Redis/RQ if you need thousands of jobs a minute.
- Environment variable values are stored in plain text in the database (OAuth tokens are encrypted). Encrypt them too if the database is not already protected.

## Detailed feature status

See [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md) for the implemented/partial/not implemented matrix across all requested platform modules. The matrix distinguishes working local Codeskys functionality from features that still require provider integrations or runtime infrastructure.
