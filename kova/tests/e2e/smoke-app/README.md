# Codeskys smoke-test app (disposable)

The smallest app for the live Codeskys → Coolify test (Phase 4/5). Not a student
repository. Nothing here is deployed or published until the owner approves.

- Codeskys framework: **Static HTML** (no install or build step). Codeskys generates
  the Dockerfile (nginx-unprivileged on 8080), so the repository needs only `index.html`.
- Success check: the deployment URL returns HTTP 200 with the text `codeskys-smoke-ok`.
- Failure check: a branch whose framework setting is Node.js with start command
  `node missing.js` makes the app exit at start. Codeskys must report Failed
  ("did not become available"), stop the app, and never show a URL as live.
- Expiry check: set this test project's TTL to 1 hour. The URL must show
  "Deployment expired" (HTTP 410) at the expiry time.

To publish (needs approval): create a public repository with just `index.html`
from this folder.
