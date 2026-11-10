# Codeskys feature status — updated 6 November 2026

This matrix describes the current source code. A navigation item or configuration screen does not mean the connected third-party service is live.

| Module | Current implementation | What is still required |
|---|---|---|
| Public landing page | Codeskys branded landing page, responsive layout, feature catalogue, accessible desktop dropdowns and mobile navigation | Browser visual QA on the owner's Windows machine |
| Student accounts | Registration, login, logout, password hashing, session and role checks | Email delivery for verification and password reset |
| Admin accounts | First admin setup through CLI or configured bootstrap environment | Owner must create the first admin locally |
| GitHub | OAuth, repository/branch browsing, public/private account access | Real OAuth App credentials and public callback URL for remote access |
| GitLab | OAuth, repository/branch browsing and webhook verification | Real GitLab application credentials and public callback URL |
| Bitbucket | Not implemented | OAuth/API adapter, repository browser, branch support, webhooks and tests |
| Azure DevOps | Not implemented | Microsoft identity/PAT authorization, organization/repository browser, webhook support and tests |
| Cursor | Supported workflow is push from Cursor to a connected Git provider | No direct Cursor hosting-provider API exists; deploy through Git provider |
| Codeskys authorization (authz) | Server-side client for authz `/internal/website/*` (X-Website-Key, v2 errors, legacy shapes tolerated); existing students link with their current credential (verified by authz, never stored, rate-limited); admin link/register/suspend; GitHub-verified repository id required before a real build; suspended/revoked/mismatched projects refused with plain messages and a REQ reference; credentials revoked on failure, cancel, expiry, delete, relink; runtime secret only ever written as a 0600 file for a runtime-proxy sidecar | authz website API + `control` network not yet deployed on the VPS (awaiting approval); runtime-proxy sidecar for Coolify apps not built; repository picker is admin-only until GitHub user authorization proves installation ownership |
| Coolify deployments | API adapter, deployment requests, state tracking, cancellation, safe teardown; start/stop/restart and live status (POST with GET fallback); private repos through the read-only GitHub App source; dedicated destination network; HTTP-only mode; per-app memory/CPU limits. Tested against a mock Coolify only | **Coolify is not installed** (owner decision 2026-11-06: prepare only). See docs/COOLIFY_INSTALL_PLAN.md. Then live end-to-end test with a dedicated test repository |
| Simulator | Explicitly labelled simulation; does not publish real websites | Use only for local workflow testing |
| Durable jobs | SQLite-backed leased queue, retries, recovery and expiry worker | Production worker process supervision and server capacity planning |
| 24-hour expiry | Persisted expiry and stop/delete flow with ownership guard; expired sites are blocked by Codeskys | Live Coolify test; DNS/CDN caching must also be configured correctly in production |
| Deploy previews | Preview deployment target and history in Codeskys | Provider and pull/merge-request webhook wiring for automatic previews |
| Build logs and analytics | Build logs, deployment outcomes, build durations and project activity | Runtime traces, request metrics and actual resource telemetry need instrumentation/provider support |
| Environment variables | Project-scoped values, secret masking and encryption at rest | Production encryption key must be backed up and protected |
| Domains | Project domain records, verification workflow and system-domain handling | Wildcard DNS, public domain and certificate/provider configuration |
| Project Forms | Form builder API, public submissions, validation, rate limiting, inbox and CSV export | Spam controls beyond honeypot/rate limit; email notifications need email provider |
| DevOps hooks | Signed GitHub/GitLab webhook handlers, delivery dedupe and deploy hooks | Public HTTPS endpoint and repository webhook setup |
| Cloud compute | CPU/memory settings and Coolify integration hooks | Live metrics depend on provider support and a configured server |
| Web security | Password hashing, CSRF, rate limits, role/ownership checks, secret encryption and security headers | External WAF, DDoS, dependency and secret scanning integrations; independent security review |
| Identity & analytics | Codeskys account identity and project analytics | Application end-user identity service is not implemented |
| Managed databases | Not implemented | Isolated PostgreSQL/MySQL/Redis provisioning, credentials, backup and restore adapters |
| Object storage / volumes | Not implemented | Storage provider, per-project isolation, quota and backup lifecycle |
| Serverless Functions | Not implemented | Sandboxed function runtime, invocation gateway, scheduler and lifecycle management |
| AI Gateway | Not implemented | Secure model proxy, provider adapters, key management, routing, metering and tests |
| General AI Agent Runners | Only deployment build workers exist | Coding-agent integrations, workspace sandbox, patch review and approval workflows |
| Edge Network / CDN | Not implemented | CDN/edge provider, domain delegation, caching, purge and edge analytics |
| Notifications | In-app notifications | SMTP/email or other delivery provider for email/Slack notifications |
| Billing | Razorpay test-mode checkout for the Student plan (₹500 / 6 months, from the landing page): server-side catalog pricing, checkout signature + provider confirmation, signed idempotent replay-safe webhooks, pending/failed/paid/refunded states, renewal, billing history, receipts, admin refunds, plan expiry (docs/BILLING.md). 24 tests against a fake Razorpay API | Owner's Razorpay **test** keys + webhook secret for a real sandbox run; public HTTPS URL for webhooks; owner to confirm prices (₹500 landing vs ₹510 in-app); live mode needs KYC + explicit approval; GST invoices not implemented |

## Verification on 6 November 2026

- **Local end-to-end (real containers, Docker on the owner's PC): 19/19 PASS** via
  `forgeops/ops/e2e/run_e2e.py`. It covers the website client → authz (website API) →
  generated compose → runtime sidecar → Data API: authorized path, app isolation (no route
  to authz/Data API/postgres), cross-tenant sidecar refusal, suspend/restore, forged
  secret, supersede, revoke (refused in ~4 s), secret file cleanup. Non-student test
  project only (octocat/Hello-World). Not Coolify: the compose file is run by plain
  `docker compose`; the Coolify transport of it is unverified until Coolify exists.
- Website suite after the sidecar work: **177 passed** (+25 hostile-input subtests).

- `python -m pytest`: **162 passed** (130 existing incl. 3 stale tests updated to the rebranded landing page and the 1-project Hobby limit; 1 new plan-limit test; 31 new authz/Coolify integration tests).
- All integration tests run against fakes of authz and Coolify. They prove the contract and the secret-handling rules, **not** live behaviour.
- Local smoke run of the website: new endpoints return the expected plain-language errors when authz/Coolify are absent.
- Not done: browser visual QA of the two new Settings cards (the browser extension was not connected).

## Local verification performed for this package (2 November 2026)

- `python -m compileall -q .` — passed.
- `node --check kova/static/js/app.js` — passed.
- All four SQLite migrations were applied to a fresh temporary database; schema version 4 and the new form tables/column were present; `PRAGMA foreign_key_check` reported no issues.
- There are 130 test methods in the test suite, including six new form tests. The test suite was **not run** in this packaging environment because Flask is not installed and the environment could not reach the package index.
- No live Coolify, GitHub, GitLab, payment, DNS, email, database, CDN or AI provider integration was available for end-to-end verification.
