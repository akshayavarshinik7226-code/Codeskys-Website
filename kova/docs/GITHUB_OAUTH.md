# GitHub sign-in / repository connection (OAuth App). PREPARED, needs owner credentials

Needs public HTTPS first (Phase E): GitHub sends the user back to the callback URL.

## 1. Owner creates the OAuth App (github.com → Settings → Developer settings → OAuth Apps → New)
| Field | Value |
|---|---|
| Application name | Codeskys |
| Homepage URL | `https://codeskys.com` |
| Authorization callback URL | `https://codeskys.com/api/git/github/callback` |
| Enable Device Flow | off |
Then "Generate a new client secret". Copy the **Client ID** and the **client secret**.
Do not paste the secret into chat, a ticket or the website's Integrations screen.

## 2. Put them on the server (VPS, as root; values never echoed)
```bash
install -m 0640 -o root -g 10001 /dev/null /etc/codeskys/secrets/github_oauth_client_secret
read -rs S && printf %s "$S" > /etc/codeskys/secrets/github_oauth_client_secret; unset S   # paste, Enter
# website.env:
GITHUB_CLIENT_ID=<client id>                         # not secret
GITHUB_CLIENT_SECRET_FILE=/run/secrets/github_oauth_client_secret
KOVA_PUBLIC_URL=https://codeskys.com                 # set by Phase E
```
Mounting the file into the website container is one compose `secrets:` entry (same pattern as
`github_app_key` for authz). It ships with the phase that enables sign-in, with its own approval.

## 3. Permissions requested (least privilege)
Default scope **`read:user`**: profile, public repositories and branches. The token is used only
for GET requests (`/user`, `/user/repos`, `/repos/{repo}/branches`). Private repositories are read
through the **read-only `codeskys-deploy` GitHub App** installation (verified by authz), never with
this token. GitHub's `repo` scope would grant **write** access to every private repository the
user has, so it is not requested. Opt-in only: `KOVA_GITHUB_OAUTH_SCOPE="repo read:user"`.
Tests: `tests/test_oauth_scope.py`.

## 4. Verify after enabling
- Settings → Git providers shows GitHub "configured"; Connect → GitHub consent screen lists
  only "Read-only access to your profile" (no repository write).
- Callback returns to codeskys.com; repository list loads; a forged `state` is refused (existing tests).
- `GET /api/git/providers` never contains the client secret.
