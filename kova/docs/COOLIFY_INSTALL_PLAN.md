# Coolify v4 on the Codeskys VPS: install and rollback plan

**Status (2026-11-06): PREPARED ONLY. Nothing in this document has been run.**

> **2026-11-12: partly superseded** by forgeops `ops/engine/COOLIFY_SINGLE_VPS_PLAN.md` (pinned
> 4.7.5, HTTPS edge, three dedicated networks, compose-only builds, Coolify backup). Use that plan's
> order of steps. Corrected here: the fingerprint file is `/root/baseline48.sql` (there is no
> `/root/baseline.sql`), and `/opt/codeskys/forgeops` is **not** a git checkout, so every file a
> step changes is copied to `/root/pre-coolify/` in step 0 and restored from there.
The owner chose "Prepare only". Both Claude sessions working on Codeskys follow
that decision. Installing needs a new, explicit approval.

Server: Bluehost VPS `129.121.169.143`, Ubuntu 26.04.1, 4 vCPU / 7.7 GB RAM / 193 GB
disk (3 % used), Docker 29.9.3, Compose v5.7.0.

---

## 1. What was verified before writing this plan (read-only)

| Check | Result |
|---|---|
| Latest backup | `/var/backups/codeskys/20261106T062316Z` (and the peer's 06:34 run): checksums, gzip, **test restore** and credential-hash digest all PASS. An encrypted copy is pulled to the PC. |
| Student data | 48 students / 48 projects / 48 authorizations (created 2026-11-01). Per-table md5 fingerprints are recorded. Prod also holds 2 platform test projects (`private-keys`, `Hello-World` demo). |
| Installer reviewed | `install.sh` 1196 lines, sha256 `c221f81e…f137`; `upgrade.sh` `39d16cbb…9a02`; `docker-compose.prod.yml` `f8b104aa…1ff5d`. Version 4.6.0. **Re-download and compare the hashes before running it; re-review if they changed.** |
| Docker daemon | `daemon.json` has no `default-address-pools`. The 4.6.0 installer wants to add one, **but skips the change and the Docker restart while containers are running** (unless `DOCKER_POOL_FORCE_OVERRIDE=true`). So no Docker restart is expected. `live-restore: true` is a second safety net. |
| Docker install | Not reinstalled: 29.9.3 is above the minimum (24). The installer does not gate on the Ubuntu version. |
| Names | Coolify creates `coolify`, `coolify-db`, `coolify-redis`, `coolify-realtime`, `coolify-proxy` and network `coolify`. No clash with `codeskys-*` / `codeskys_*`. Data goes under `/data/coolify` (does not exist today). |
| Ports | Coolify publishes **8000, 6001, 6002 on 0.0.0.0** by default. ufw does not filter Docker-published ports, but the existing `codeskys-docker-guard` DOCKER-USER chain drops every inbound port except 80/8080 on `ens3`, so they would not be public. We also bind them to 127.0.0.1 (step 3). |
| Proxy clash | Coolify's Traefik (`coolify-proxy`) wants host **80/443/8080**, which Caddy already holds. It must be reconfigured to have **no host ports** before it is started (step 6). |
| Firewall | ufw allows 22, 80, 8080 only. DOCKER-USER guard is active. |
| SSH | Root login by key is already enabled (Coolify needs it). Password auth is off. |

## 2. Security implications (read this before approving)

1. **Coolify is root on this server.** The installer generates an SSH key and
   appends its public half to `/root/.ssh/authorized_keys`. The Coolify container
   uses it to run commands on the host **as root**, including `docker` (which is
   itself root-equivalent). Anyone who controls the Coolify dashboard, its API
   token, its database or its container can read every secret on the VPS:
   `/etc/codeskys/*`, the authz database with the 48 student credential hashes,
   the GitHub App private key, and backups. Coolify cannot work without this.
2. **Untrusted student code builds and runs on the same machine** as the student
   records. A container escape or kernel bug in a build would expose everything
   above. Docker isolation plus resource limits reduce this risk; they do not remove it.
3. **Coolify's API token** (used by the website) is as powerful as the dashboard.
   It must live only in `/etc/codeskys/website.env` (root 0600). The website must
   never store it in its database (set `COOLIFY_TOKEN` as an env var, not through
   the Integrations screen).
4. **Supply chain.** The installer downloads and runs images from Docker Hub/GHCR
   as root. Auto-update would pull new code as root without review, so we turn it
   off and upgrade deliberately.

**Mitigations in this plan:** dashboard reachable only through an SSH tunnel (all
Coolify ports bound to 127.0.0.1 plus the DOCKER-USER guard); `AUTOUPDATE=false`;
version pinned; strong unique admin password plus 2FA in Coolify; a scoped API
token; student apps on a **separate destination network** with no route to
Coolify's DB/Redis/API or to `codeskys_platform`/`control`; per-app memory/CPU
limits; one concurrent build; Traefik with no host ports, behind Caddy.

**Stronger option (costs money):** run Coolify on a second VPS and keep this one
for the website, authz and student data. Then a compromised Coolify cannot read
the student credential database. The website code needs no change: only
`COOLIFY_URL` differs.

## 3. Install steps (only after approval)

Coordinate first: message the other session (`forgeops-5c`) and wait for its OK.
Then do everything in one SSH session as root.

```bash
# 0. Pre-flight
cd /opt/codeskys/forgeops
bash ops/vps/backup.sh                         # must end with "verify PASS"
bash ops/vps/healthcheck.sh; echo "health=$?"  # must be 0
iptables -S DOCKER-USER > /root/pre-coolify-docker-user.txt
cp -a /etc/docker/daemon.json /root/pre-coolify-daemon.json
cp -a /root/.ssh/authorized_keys /root/pre-coolify-authorized_keys
docker ps --format '{{.Names}} {{.Status}}' > /root/pre-coolify-containers.txt
# Files later steps change. Not a git checkout: these copies ARE the rollback.
install -d -m 0700 /root/pre-coolify
cp -a ops/vps-https .env /etc/codeskys/codeskys.env /etc/codeskys/website.env /root/pre-coolify/
cp -a /etc/ufw/user.rules /etc/ufw/user6.rules /root/pre-coolify/
# student fingerprint: must equal the recorded baseline
docker compose exec -T postgres psql -U forge -d forgeops -tA < /root/baseline48.sql | tee /root/pre-coolify/fp.txt
diff /root/pre-coolify/fp.txt /root/step2-20261112T075247Z/fp.before && echo fingerprint-equals-baseline

# 1. Fetch and verify the installer (do NOT pipe curl into bash)
curl -fsSL https://cdn.coollabs.io/coolify/install.sh -o /root/coolify-install.sh
sha256sum /root/coolify-install.sh   # expect c221f81e0bc9b47541f47bf671da76c79cfb61ce4741c3cb0800d7a18b99f137

# 2. Bind every Coolify port to loopback BEFORE first start
install -d -m 0700 /data/coolify/source
cat > /data/coolify/source/docker-compose.custom.yml <<'YAML'
services:
  coolify:
    ports: !override
      - "127.0.0.1:8000:8080"
      - "127.0.0.1:6001:6001"
      - "127.0.0.1:6002:6002"
YAML
# In 4.6.0 all three published ports belong to the "coolify" service ("soketi" is an
# empty placeholder). Re-check after download:
#   docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.custom.yml config | grep -A4 ports

# 3. Install, pinned, no auto-update, never force the Docker daemon change
AUTOUPDATE=false bash /root/coolify-install.sh 4.6.0
#   expect: "Docker daemon configuration not changed: ... containers running"
#   STOP and roll back if it prints "restarting Docker daemon".

# 4. Verify nothing public changed
ss -tlnp | grep -E ':(8000|6001|6002)\b'         # must show 127.0.0.1 only
diff <(iptables -S DOCKER-USER) /root/pre-coolify-docker-user.txt && echo guard-unchanged
cmp /etc/docker/daemon.json /root/pre-coolify-daemon.json && echo daemon-unchanged
bash ops/vps/healthcheck.sh; echo "health=$?"     # still 0
```

From the PC: `ssh -L 8000:127.0.0.1:8000 root@129.121.169.143`, then open
`http://127.0.0.1:8000`. Create the admin account immediately (the first visitor
becomes admin; the tunnel guarantees that is us). Then enable 2FA.

```text
5. Coolify settings (dashboard, via the tunnel)
   Settings → Auto update: OFF.  Instance domain: none (tunnel only).
   Servers → localhost → General: "Concurrent builds" = 1; Build timeout 1800 s.
   Servers → localhost → Destinations → Add: Docker network "codeskys-apps".
     Note its UUID -> COOLIFY_DESTINATION_UUID.
   Projects → New "codeskys-students", environment "production".
     Note project UUID + server UUID.
   Keys & Tokens → API tokens → new token, permissions: read + write + deploy
     (NOT root / "read:sensitive"). Shown once -> straight into website.env.
   Sources → GitHub App → "Add existing": App ID 5637966, installation 179150342,
     client id/secret + private key of codeskys-deploy. Permissions stay
     contents:read + metadata:read; Coolify must NOT be allowed to create a new App
     with write scopes. Note its UUID -> COOLIFY_GITHUB_APP_UUID.
```

```bash
# 6. Traefik: internal only, behind Caddy. In the dashboard: Servers → localhost →
#    Proxy → Configuration, BEFORE pressing Start, change the proxy compose so that
#    coolify-proxy has NO "ports:" entries (remove 80/443/8080), keeps the
#    "coolify" network, and also joins "codeskys-apps". Then Start the proxy.
docker inspect coolify-proxy --format '{{json .HostConfig.PortBindings}}'   # must be {}

# 7. SUPERSEDED (HTTP-era): the VPS now runs ops/vps-https/. Use the single-VPS plan §9.6
#    (Caddyfile.https.coolify + docker-compose.coolify.yml). Kept for reference only.
#    Caddy forwards test-app hostnames to Traefik (brief Caddy recreate: ~2 s)
#    ops/vps-http/Caddyfile.http, ABOVE the ":80" site:
#      http://*.129-121-169-143.sslip.io {
#          import common
#          reverse_proxy coolify-proxy:80
#      }
#    and in docker-compose.vps-http.yml give caddy the extra external network
#    "codeskys-apps" (declared `external: true`). Then:
docker compose up -d --no-deps caddy
curl -s -o /dev/null -w '%{http_code}\n' http://129.121.169.143/          # website: 200
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: x.129-121-169-143.sslip.io' http://127.0.0.1/  # 404 from Traefik
```

`*.129-121-169-143.sslip.io` is free public wildcard DNS that resolves to the VPS
IP, so test apps get working hostnames today without touching codeskys.com DNS.
Replace it with `*.apps.codeskys.com` (plus a wildcard certificate) once codeskys.com
points at the VPS.

```bash
# 8. Website configuration (/etc/codeskys/website.env, root 0600; values never printed)
COOLIFY_URL=http://coolify:8080          # website joins the "coolify" network, or use host tunnel
COOLIFY_TOKEN=<token from step 5>
COOLIFY_PROJECT_UUID=<…>  COOLIFY_SERVER_UUID=<…>
COOLIFY_DESTINATION_UUID=<codeskys-apps uuid>
COOLIFY_GITHUB_APP_UUID=<…>
COOLIFY_BASE_DOMAIN=129-121-169-143.sslip.io
COOLIFY_FORCE_HTTPS=0                    # until HTTPS exists
KOVA_BUILD_MEMORY=512M  KOVA_BUILD_CPUS=0.5
# then, as admin in the dashboard: Integrations → Coolify → Test connection → engine "Coolify"
docker compose up -d --no-deps website   # website recreate: a few seconds
```

## 4. Network isolation for student apps (target state)

```
internet ─▶ Caddy :80 ─┬─ codeskys website (edge)
                       ├─ :8080 ForgeOps demo (edge)
                       └─ *.sslip.io ─▶ coolify-proxy (Traefik, no host ports) ─▶ student apps
                                                         network: codeskys-apps
student apps ──X── coolify-db / coolify-redis / coolify API   (different network)
student apps ──X── codeskys_platform (authz, postgres, Data API), control, edge
student apps ───▶ internet (egress, needed for package installs)
```
Verify after the first deployment, from inside the test app container:
`getent hosts coolify-db authz postgres` → no answer; `wget -T3 http://authz:8010` → fails.

## 5. Protected Codeskys services for Coolify apps: still to build

The contract (`forgeops/authz/WEBSITE_INTEGRATION.md`) allows the per-deployment
`runtime_secret` only as a **file read by that deployment's runtime proxy**, never
as an env var (Coolify env vars are visible to build steps and to the app). The
website already implements this hand-off (`CODESKYS_RUNTIME_SECRET_DIR`,
`kova/runtime_secrets.py`, 0600 file, removed on revoke/expiry/supersede), and it
**requests no credential while the directory is unset**.

What is missing is the sidecar itself: a runtime-proxy container per Coolify app
that mounts `<dir>/<dep_id>/runtime_secret` read-only, sits on `codeskys-apps`
(for the app) and on a network that reaches authz, and is the only thing the app
talks to (`CODESKYS_RUNTIME_URL`). Recommended route: deploy each app with
Coolify's *Docker Compose* build pack, using a compose file **generated by
Codeskys** (app service from the student repo + `codeskys-runtime` sidecar), so the
student repository is still never modified. Until then, Coolify deployments run
fine but protected services refuse them, which the build log states.

**Owner of the sidecar:** the ForgeOps/authz session, reusing the existing
`runtime/` proxy image (`runtime/proxy.py`), only after the owner approves that
phase. Its interface:

| Sidecar setting | Value |
|---|---|
| `CODESKYS_DEPLOYMENT_ID`, `CODESKYS_REPOSITORY_ID` | non-secret env |
| `CODESKYS_RUNTIME_SECRET_FILE` | path of `<dir>/<dep_id>/runtime_secret`, mounted **read-only** |
| `AUTHZ_INTERNAL_URL` | authz (`/internal/runtime/token` needs only the secret, no key) |
| `DATA_API_URL` | the private Data API |
| app env `CODESKYS_RUNTIME_URL` | `http://codeskys-runtime:8090` |

**Private subnets.** Each project's private network gets `pool base + project_id × 16`
(a /28) from `CODESKYS_RUNTIME_SUBNET_POOL` (default `10.213.0.0/16`, 4095 projects).
Website project ids are SQLite `AUTOINCREMENT`, so each subnet is exclusive to one
project. The sidecar's client-CIDR gate depends on that exclusivity. Conditions:
* The pool must be **excluded from Docker's `default-address-pools`** and from any
  network Coolify creates. Today `daemon.json` has no pools, and the defaults
  (172.17–31.x, 192.168.x) do not overlap. Re-check this after installing Coolify.
* Each website instance on one Docker host (e.g. staging and prod) needs its
  **own, non-overlapping pool** and its own `CODESKYS_WEBSITE_INSTANCE`.
* If `kova.db` is restored from an older backup, ids can repeat while old networks
  still exist. Docker then refuses the overlapping subnet and the deploy **fails**.
  That is intended. Never retry with another subnet, and never attach a network
  whose `codeskys.project_id` / `codeskys.website_instance` labels belong to another
  project. Remove the stale network deliberately.

**Network rule for the generated compose:** one internal network per app shared
by `{app, sidecar}` only, plus a separate network that only the sidecar shares
with authz and the Data API. The student app container must have no route to
authz or the Data API. Student code is never attached to `platform` or `control`.

## 6. Rollback (full removal; student data is not touched at any point)

Coolify keeps all of its state in its own containers, volumes and `/data/coolify`.
It never touches the `codeskys_*` volumes, `/etc/codeskys` or `/srv/codeskys`.

```bash
cd /data/coolify/source
# 1. remove apps Coolify created (only ours exist; check the list first)
docker ps -a --filter label=coolify.managed=true --format '{{.Names}}'
docker ps -aq --filter label=coolify.managed=true | xargs -r docker rm -f
# 2. stop Coolify and its proxy
docker compose --env-file .env -f docker-compose.yml -f docker-compose.prod.yml \
  $( [ -f docker-compose.custom.yml ] && echo -f docker-compose.custom.yml ) down
docker rm -f coolify-proxy coolify-sentinel 2>/dev/null
# 3. archive, then remove its data (keep the archive until the owner says otherwise)
tar czf /var/backups/coolify-removed-$(date -u +%Y%m%dT%H%M%SZ).tgz -C / data/coolify
docker volume ls -q | grep -E '^coolify' | xargs -r docker volume rm
docker network rm coolify codeskys-apps 2>/dev/null
rm -rf /data/coolify
# 4. revoke Coolify's root SSH access
cp /root/.ssh/authorized_keys /root/authorized_keys.with-coolify
sed -i -E '/[[:space:]](root@)?coolify[[:space:]]*$/d' /root/.ssh/authorized_keys
diff /root/.ssh/authorized_keys /root/pre-coolify-authorized_keys && echo keys-restored
# 5. put back the files saved in step 0 (no git here), then recreate what used them
cd /opt/codeskys/forgeops
rm -rf ops/vps-https && cp -a /root/pre-coolify/vps-https ops/vps-https
cp -a /root/pre-coolify/.env .env
cp -a /root/pre-coolify/codeskys.env /root/pre-coolify/website.env /etc/codeskys/
docker compose up -d --no-deps caddy website authz dataapi   # the four the overlay touched
# 6. verify
cmp /etc/docker/daemon.json /root/pre-coolify-daemon.json && echo daemon-unchanged
diff <(iptables -S DOCKER-USER) /root/pre-coolify-docker-user.txt && echo guard-unchanged
bash ops/vps/healthcheck.sh; echo "health=$?"
docker compose exec -T postgres psql -U forge -d forgeops -tA < /root/baseline48.sql | diff - /root/pre-coolify/fp.txt && echo fingerprint-unchanged
```
Also revoke the Coolify API token and the GitHub App client secret that were
given to Coolify (GitHub → App settings → regenerate client secret).

If the installer **did** restart Docker (it should not): containers come back on
their own (`restart: unless-stopped`, `live-restore`). Check with
`docker compose ps` and `healthcheck.sh`. If `daemon.json` changed, restore it
from `/root/pre-coolify-daemon.json` only in a planned window, because that needs
another Docker restart.

## 7. First end-to-end test (after install)

Use a **new, dedicated, non-student test repository** (not `private-keys`, and none
of the 48). Suggested: a tiny public static or Flask app owned by the Codeskys
account. Then:
create project → link/register with authz (admin) → deploy → open
`http://<slug>.129-121-169-143.sslip.io` → stop / start / restart → force a build
failure (bad build command) → set and use an env var → let it expire, or delete →
confirm the Coolify app is gone and the authz deployment is revoked → re-run the
48-row fingerprint.
