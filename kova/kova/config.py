import os
import secrets
from pathlib import Path

from .crypto import load_key

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path=None):
    """Tiny .env loader (no extra dependency). Real environment wins."""
    path = Path(path or ROOT / ".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _secret_key(instance_dir: Path, env: str) -> str:
    configured = os.environ.get("KOVA_SECRET_KEY", "").strip()
    if configured:
        return configured
    if env == "production":
        raise RuntimeError("KOVA_SECRET_KEY must be set when KOVA_ENV=production")
    key_file = instance_dir / "secret_key"
    if key_file.is_file():
        return key_file.read_text(encoding="utf-8").strip()
    instance_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    key_file.write_text(key, encoding="utf-8")
    return key


def _file_or_env(name: str) -> str:
    """``NAME_FILE`` (a mounted secret file) wins over ``NAME``."""
    path = os.environ.get(f"{name}_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get(name, "").strip()


def build_config() -> dict:
    env = os.environ.get("KOVA_ENV", "development").lower()
    instance = Path(os.environ.get("KOVA_INSTANCE_DIR") or ROOT / "instance")
    instance.mkdir(parents=True, exist_ok=True)
    return {
        "ENV_NAME": env,
        "PLATFORM_NAME": os.environ.get("KOVA_PLATFORM_NAME", "Codeskys"),
        "SECRET_KEY": _secret_key(instance, env),
        "DATABASE": str(instance / "kova.db"),
        "INSTANCE_DIR": str(instance),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": os.environ.get("KOVA_COOKIE_SECURE", "0") == "1",
        "ENCRYPTION_KEY": load_key(instance, env),
        # Coolify. Environment variables win over anything stored in the database.
        "COOLIFY_URL": os.environ.get("COOLIFY_URL", "").strip().rstrip("/"),
        "COOLIFY_TOKEN": os.environ.get("COOLIFY_TOKEN", "").strip(),
        "COOLIFY_PROJECT_UUID": os.environ.get("COOLIFY_PROJECT_UUID", "").strip(),
        "COOLIFY_SERVER_UUID": os.environ.get("COOLIFY_SERVER_UUID", "").strip(),
        "COOLIFY_BASE_DOMAIN": os.environ.get("COOLIFY_BASE_DOMAIN", "").strip().lower().strip("."),
        "COOLIFY_PRIVATE_KEY_UUID": os.environ.get("COOLIFY_PRIVATE_KEY_UUID", "").strip(),
        # Coolify "GitHub App" source (private repositories through the read-only
        # codeskys-deploy App, instead of a per-repository deploy key).
        "COOLIFY_GITHUB_APP_UUID": os.environ.get("COOLIFY_GITHUB_APP_UUID", "").strip(),
        # Coolify destination (Docker network) for student apps; see docs/COOLIFY_INSTALL_PLAN.md.
        "COOLIFY_DESTINATION_UUID": os.environ.get("COOLIFY_DESTINATION_UUID", "").strip(),
        # Until codeskys.com has HTTPS, test apps are served over plain HTTP.
        "COOLIFY_FORCE_HTTPS": os.environ.get("COOLIFY_FORCE_HTTPS", "1") == "1",
        # Caddy terminates TLS for app hostnames (on-demand certificates, approved by
        # /internal/tls-ask). Coolify/Traefik then get plain http:// domains, so Traefik
        # never requests certificates or redirects, while students see https:// URLs.
        "COOLIFY_TLS_AT_EDGE": os.environ.get("COOLIFY_TLS_AT_EDGE", "0") == "1",
        # A deployment is "ready" only once its app runs and answers HTTP. Seconds to wait
        # after the build, and an optional in-server probe target (Traefik, e.g.
        # http://coolify-proxy:80) used with the app's Host header instead of its public URL.
        "COOLIFY_READY_TIMEOUT": int(os.environ.get("COOLIFY_READY_TIMEOUT", "180") or 180),
        "COOLIFY_PROBE_URL": os.environ.get("COOLIFY_PROBE_URL", "").strip().rstrip("/"),
        # Codeskys authorization service (ForgeOps authz), see
        # forgeops/authz/WEBSITE_INTEGRATION.md. Server-side only; the website key opens
        # /internal/website/* only and never reaches a browser.
        "AUTHZ_URL": os.environ.get("CODESKYS_AUTHZ_URL", "").strip().rstrip("/"),
        "AUTHZ_WEBSITE_KEY": _file_or_env("CODESKYS_AUTHZ_WEBSITE_KEY"),
        # 1 = a real (Coolify) deployment is refused unless the project is linked to authz
        #     and its repository is GitHub-verified.
        "AUTHZ_REQUIRED": os.environ.get("CODESKYS_AUTHZ_REQUIRED", "0") == "1",
        # Where deployment-bound runtime secrets are handed to the runtime-proxy
        # sidecar: a root-owned directory mounted (read-only) into that sidecar only.
        # Empty = no runtime credential is requested at all, so none can leak.
        # NEVER a build env var, the student app's env, the website DB or a log.
        "RUNTIME_SECRET_DIR": os.environ.get("CODESKYS_RUNTIME_SECRET_DIR", "").strip(),
        # Non-secret URL of the runtime proxy as seen from a deployed app (sidecar).
        "RUNTIME_URL": os.environ.get("CODESKYS_RUNTIME_URL", "").strip().rstrip("/"),
        # Member-less group that may read runtime secret files (the sidecar gets it
        # via group_add). Empty = files are owner-only (0600).
        "RUNTIME_SECRET_GID": os.environ.get("CODESKYS_RUNTIME_SECRET_GID", "").strip(),
        # Sidecar wiring for generated deployments (docs/COOLIFY_INSTALL_PLAN.md §5).
        "RUNTIME_IMAGE": os.environ.get("CODESKYS_RUNTIME_IMAGE", "").strip(),
        "RUNTIME_SECRET_HOST_DIR": os.environ.get("CODESKYS_RUNTIME_SECRET_HOST_DIR", "").strip(),
        "APPS_NETWORK": os.environ.get("CODESKYS_APPS_NETWORK", "codeskys-apps").strip(),
        "RUNTIME_LINK_NETWORK": os.environ.get("CODESKYS_RUNTIME_LINK_NETWORK", "codeskys-runtime-link").strip(),
        # MUST differ per website instance on one Docker host (prod vs staging), and must
        # be excluded from Docker/Coolify default-address-pools.
        "RUNTIME_SUBNET_POOL": os.environ.get("CODESKYS_RUNTIME_SUBNET_POOL", "10.213.0.0/16").strip(),
        "WEBSITE_INSTANCE": os.environ.get("CODESKYS_WEBSITE_INSTANCE", "default").strip().lower(),
        # Git provider OAuth. Secrets stay server-side and are never sent to the browser.
        "GITHUB_CLIENT_ID": os.environ.get("GITHUB_CLIENT_ID", "").strip(),
        "GITHUB_CLIENT_SECRET": _file_or_env("GITHUB_CLIENT_SECRET"),
        "GITHUB_OAUTH_SCOPE": os.environ.get("KOVA_GITHUB_OAUTH_SCOPE", "").strip(),
        "GITLAB_CLIENT_ID": os.environ.get("GITLAB_CLIENT_ID", "").strip(),
        "GITLAB_CLIENT_SECRET": _file_or_env("GITLAB_CLIENT_SECRET"),
        "PUBLIC_URL": os.environ.get("KOVA_PUBLIC_URL", "").strip().rstrip("/"),
        "LOGIN_MAX_FAILURES": 5,
        "LOGIN_LOCK_MINUTES": 15,
        "VERSION": "1.1.0",
        "SIM_SPEED": float(os.environ.get("KOVA_SIM_SPEED", "1")),
        "SYSTEM_DOMAIN": os.environ.get("KOVA_SYSTEM_DOMAIN", "codeskys.com"),
        "GIT_LOOKUPS": os.environ.get("KOVA_GIT_LOOKUPS", "1") == "1",
        # Deployment lifetime and per-build resource ceilings.
        "DEFAULT_TTL_HOURS": int(os.environ.get("KOVA_TTL_HOURS", "24")),
        "BUILD_MEMORY": os.environ.get("KOVA_BUILD_MEMORY", "512M").strip(),
        "BUILD_CPUS": os.environ.get("KOVA_BUILD_CPUS", "0.5").strip(),
        # Run the queue worker inside the web process. Convenient for one machine;
        # set to 0 and run `python manage.py worker` separately in production.
        "INLINE_WORKER": os.environ.get("KOVA_INLINE_WORKER", "1") == "1",
    }
