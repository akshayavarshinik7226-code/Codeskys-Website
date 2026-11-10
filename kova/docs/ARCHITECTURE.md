# Codeskys architecture

## Request path

```
Browser (vanilla JS SPA)
  │  same-origin JSON API
  │  session cookie + X-CSRF-Token, or Authorization: Bearer kova_…
  ▼
Flask application  (kova/)
  ├── __init__.py   app factory, bearer-token auth, CSRF gate, security headers
  ├── auth.py       login/logout/me, login_required + admin_required
  ├── pages.py      login page and the authenticated app shell
  ├── projects.py   projects, deployments, env vars, domains, deploy hooks, analytics
  ├── account.py    profile, password, API tokens, notifications, activity feed
  ├── admin.py      member management, Coolify connection, engine selection
  ├── engine.py     deployment state machine + simulator and Coolify providers
  ├── gitinfo.py    Git URL parsing, GitHub metadata, framework detection
  ├── sites.py      serves simulator deployments at /sites/<slug>/
  ├── providers.py  GitHub/GitLab OAuth, repository and branch browsing
  ├── webhooks.py   signed push webhooks (GitHub HMAC, GitLab token)
  ├── jobs.py       durable job queue, worker loop, expiry scheduler
  ├── crypto.py     Fernet encryption for secrets at rest
  ├── ratelimit.py  database-backed token buckets
  ├── seed.py       demo workspace generator
  ├── security.py   scrypt hashing and constant-time verification
  ├── config.py     environment configuration
  └── db.py         SQLite connection, forward-only migrations, audit, settings
```

## Data model (migration 2)

| Table | Purpose |
| --- | --- |
| `users` | accounts, role (`admin`/`student`), plan, lockout state |
| `projects` | repo, branch, framework, build settings, provider reference, hook token, pointer to the current production deployment |
| `deployments` | one row per build: status, stage, target, commit, trigger, URL, duration |
| `deploy_logs` | append-only build output, one row per line, with level and stage |
| `env_vars` | per-project key/value, secret flag, target list |
| `domains` | system subdomain plus custom domains with verification token and status |
| `api_tokens` | SHA-256 token hashes, prefix for display, optional expiry |
| `notifications` | per-user deployment and account events |
| `settings` | key/value store for the Coolify connection and engine choice |
| `audit_log` | every meaningful action, with actor, IP and project |
| `git_accounts` | one connected GitHub/GitLab account per user; tokens encrypted |
| `jobs` | the durable queue: kind, payload, state, attempts, lease |
| `webhook_deliveries` | delivery ids already processed, for replay protection |
| `rate_limits` | token buckets, so limits survive a restart |

Migrations are a numbered list in `db.py`, applied in order and recorded in `schema_version`. They are forward-only; never edit a migration that has shipped — append a new one.

## Deployment state machine

```
queued ──▶ building ──┬──▶ ready ──▶ expired   (TTL reached; application torn down)
                      ├──▶ failed
                      └──▶ cancelled
```

`projects.trigger_deployment()` inserts the deployment row and enqueues a `deploy`
job. A worker claims it under a lease and runs it. Stages (`initializing`, `cloning`, `installing`, `building`, `deploying`, `ready`) are written to `deployments.stage` as the build progresses, and the browser polls `GET /api/deployments/<id>?after=<last log id>` once a second for new log lines and the current status.

### The queue

Jobs are claimed inside `BEGIN IMMEDIATE` and held under a lease (`locked_by`,
`locked_until`), which gives three properties:

* two workers can never hold the same job;
* a worker that dies releases its job when the lease expires, so nothing is
  stranded;
* a long build keeps its claim by heartbeating, so the lease is not mistaken
  for a crash.

Failures retry with backoff up to `max_attempts`; the final failure also marks
the deployment failed, so a dead job never leaves a build spinning. On every
tick the worker also sweeps: deployments past `expires_at` get an `expire` job,
builds whose job vanished are re-queued, and builds whose job finished without
completing them are failed with an explanation.

### Expiry and teardown

A deployment that reaches `ready` is stamped with `expires_at = now + ttl_hours`.
When the sweeper enqueues its expiry, the handler tears down the Coolify
application **only if this deployment is the one currently being served**, so a
superseded build expiring does not take the live site down. Before deleting,
Codeskys fetches the remote application and compares its name with the name Codeskys
gave it; a mismatch refuses the delete and records the reason. Codeskys therefore
never removes a Coolify resource it did not create.

Only a *production* deployment that reaches `ready` updates `projects.current_deployment_id`. Promoting an older build is an instant pointer change on the simulator; on Coolify it triggers a rebuild pinned to that commit, since the container has to be recreated.

### Providers

`engine.active_provider()` returns `coolify` only when the engine is set to Coolify **and** the URL, token, project UUID and server UUID are all present — otherwise it falls back to the simulator, so a half-finished configuration can never leave a deployment stuck.

**Simulator** writes a realistic log for the project's framework and serves the result from `sites.py`. It never executes repository code.

**Coolify** calls, in order:

| Step | Coolify v1 endpoint |
| --- | --- |
| Create application (first deploy) | `POST /applications/public` |
| Sync build settings (later deploys) | `PATCH /applications/{uuid}` |
| Sync environment variables | `PATCH /applications/{uuid}/envs/bulk` |
| Trigger the build | `GET /deploy?uuid=…&force=…` |
| Poll status and logs | `GET /deployments/{uuid}` |
| Cancel | `POST /deployments/{uuid}/cancel` |
| Delete on project removal | `DELETE /applications/{uuid}` |

Build-pack selection: `dockerfile` for Dockerfile projects, `static` for static projects with no build command, otherwise `nixpacks`.

## Security

- scrypt password hashes (n=2^14), constant-time comparison, a dummy hash so unknown emails take the same time as wrong passwords
- Session cookie is HttpOnly + SameSite=Lax; the session is cleared and the CSRF token regenerated on every successful login
- Every mutating `/api/` request needs `X-CSRF-Token`, except token-authenticated calls and deploy hooks (which carry their own secret in the URL)
- Five failed logins lock the account for fifteen minutes; all attempts are audited
- Content-Security-Policy restricts scripts, fonts, images and connections to same-origin. `style-src` allows inline attributes, which the dashboard uses for layout; scripts never do
- API tokens are stored as SHA-256 hashes — the plaintext is shown once at creation
- Ownership is enforced server-side on every project route; the front end is not a trust boundary

## Untrusted repository code

The simulator never runs repository code. When Coolify is connected, builds happen inside Coolify's own container isolation on your server. Before letting untrusted users deploy, configure resource limits and network restrictions in Coolify, and never mount the host Docker socket into a build.

## Front end

Three files, no build step: `api.js` (fetch wrapper that holds the CSRF token), `icons.js` (inline SVG set), `app.js` (hash router, views, components). State lives in the DOM and a small `State` object; every page refetches on navigation. Inter is self-hosted in `static/fonts/` so the strict CSP holds and the app works offline.
