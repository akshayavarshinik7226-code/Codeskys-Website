"""Multi-service deployments from a student's docker-compose file.

The student's compose file is never handed to the deployment engine. It is read, checked and
translated into a new one that Codeskys writes from data structures (like composegen):

* services are either built from the project's own Dockerfiles (each checked by buildpolicy)
  or are one of the approved data services (PostgreSQL, MySQL, MariaDB, Redis, MongoDB);
* privileged mode, host networking/PID/IPC, added capabilities, devices, the Docker socket,
  extra hosts and intentionally vulnerable lab images are refused;
* bind mounts, custom networks and published host ports are dropped (the containers already
  hold the code; Codeskys routes the one public service itself); named volumes are kept;
* the public service is renamed ``app`` (the only name the edge routes) and every service gets
  the platform hardening and its own share of the plan's memory/CPU.

All services share the deployment's own private network, so ``postgres://db:5432`` style
addresses keep working; nothing else (no other student, no platform service) is on it.
"""
from __future__ import annotations

import re

COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
MAX_COMPOSE_BYTES = 64 * 1024
MAX_SERVICES = 6
MAX_VOLUMES = 6
# The total the services of one deployment may use (each gets its own limit below).
MAX_TOTAL_MEMORY_MB = 1536

# repository -> (memory MB, CPUs). The official images start as root, fix the ownership of
# their data directory and drop to their own user, so they need exactly these capabilities back.
DATA_IMAGES = {"postgres": (256, 0.25), "mysql": (512, 0.5), "mariadb": (384, 0.5),
               "redis": (128, 0.1), "mongo": (512, 0.5)}
DATA_CAPS = ["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER"]
WORKER_LIMITS = (256, 0.25)
DEPENDS_CONDITIONS = ("service_started", "service_healthy", "service_completed_successfully")
# Deliberately vulnerable training targets: never on the public internet (cybersecurity track).
VULNERABLE = re.compile(r"juice-?shop|\bdvwa\b|webgoat|metasploitable|mutillidae|\bbwapp\b|hackazon|vulhub|"
                        r"\bdvna\b|nodegoat|railsgoat|vulnerable-?app|damn-?vulnerable", re.I)
# Preferred names for the public service, in order.
WEB_NAMES = ("app", "web", "frontend", "client", "ui", "site", "www", "api", "backend", "server")

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_VOLUME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_TAG = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PATH = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_ABS = re.compile(r"^/[A-Za-z0-9._/@-]{0,250}$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_INTERP = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]{0,127})(?:(:?[-?])([^}$]{0,200}))?\}"
                     r"|\$([A-Za-z_][A-Za-z0-9_]{0,127})")
_TOKEN = re.compile(r"\$\$|" + _INTERP.pattern)
_DURATION = re.compile(r"^\d{1,5}(?:\.\d+)?(?:ms|s|m|h)$")

# Keys that would give a container more than an ordinary process: refused outright.
UNSAFE_KEYS = {"privileged", "network_mode", "pid", "ipc", "cap_add", "devices", "security_opt", "userns_mode",
               "sysctls", "volumes_from", "cgroup_parent", "runtime", "uts", "device_cgroup_rules", "isolation",
               "extra_hosts", "cgroup", "oom_kill_disable", "group_add"}
# Keys Codeskys sets itself or that do not apply: ignored (with a note when they matter).
IGNORED_KEYS = {"restart", "container_name", "networks", "labels", "stdin_open", "tty", "deploy", "init",
                "hostname", "domainname", "mem_limit", "memswap_limit", "cpus", "cpu_shares", "pids_limit",
                "logging", "stop_signal", "stop_grace_period", "links", "external_links", "platform",
                "pull_policy", "scale", "shm_size", "ulimits", "env_file", "tmpfs", "read_only",
                "mac_address", "dns", "dns_search", "expose", "ports", "profiles", "develop", "x-develop"}
KEPT_KEYS = {"build", "image", "command", "entrypoint", "environment", "depends_on", "healthcheck",
             "volumes", "working_dir", "user"}


class ComposePlanError(ValueError):
    """Why the compose file cannot be deployed. Always phrased for the student."""


# ---------------------------------------------------------------- reading

def parse(text: str) -> dict:
    import yaml
    if len(text.encode("utf-8", "replace")) > MAX_COMPOSE_BYTES:
        raise ComposePlanError("The docker-compose file is larger than 64 KB.")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise ComposePlanError(f"The docker-compose file is not valid YAML{where}.") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        raise ComposePlanError("The docker-compose file has no services.")
    return doc


def image_repository(image: str) -> tuple[str, str]:
    """('postgres', '16-alpine') for postgres:16-alpine, docker.io/library/postgres, ..."""
    ref = (image or "").strip().split("@")[0]
    name, tag = ref.rsplit(":", 1) if ":" in ref.split("/")[-1] else (ref, "")
    for prefix in ("docker.io/library/", "docker.io/", "library/", "index.docker.io/library/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.lower(), tag


def _text(value, what: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
        raise ComposePlanError(f"{what} must be text of at most 4096 characters.")
    return value


def escape(value: str) -> str:
    """Compose interpolates ``$``: keep ``${NAME}``, ``${NAME:-default}`` and ``$NAME`` (the project's
    own variables, written as ``${NAME}``), keep ``$$`` literal, and make every other ``$`` literal.
    Coolify's and Codeskys' own names are never interpolated."""
    out, pos = [], 0
    for m in _TOKEN.finditer(value):
        out.append(value[pos:m.start()].replace("$", "$$"))
        token = m.group(0)
        if token == "$$":
            out.append("$$")
        else:
            name = m.group(1) or m.group(4)
            reserved = name.upper().startswith(("SERVICE_", "COOLIFY_", "CODESKYS_"))
            out.append(token.replace("$", "$$") if reserved else (token if m.group(1) else "${" + name + "}"))
        pos = m.end()
    out.append(value[pos:].replace("$", "$$"))
    return "".join(out)


def _env(raw, service: str) -> dict:
    items = {}
    if raw is None:
        return items
    if isinstance(raw, list):
        for entry in raw:
            entry = _text(entry, f"An environment entry of {service}")
            key, sep, val = entry.partition("=")
            items[key.strip()] = val if sep else None
    elif isinstance(raw, dict):
        items = {str(k): v for k, v in raw.items()}
    else:
        raise ComposePlanError(f"The environment of {service} must be a list or a mapping.")
    out = {}
    for key, val in items.items():
        if not _ENV_KEY.match(key):
            raise ComposePlanError(f"{service}: {key[:40]!r} is not a valid environment variable name.")
        if key.upper().startswith(("SERVICE_", "COOLIFY_", "CODESKYS_")):
            continue            # platform names: never set by a student
        if val is None:
            out[key] = "${" + key + "}"        # "KEY" alone: the project's own variable
        else:
            out[key] = escape(_text(val, f"{service}: {key}"))
    return out


def _words(raw, what: str):
    if raw is None:
        return None
    if isinstance(raw, str):
        return escape(_text(raw, what))
    if isinstance(raw, list) and len(raw) <= 100:
        return [escape(_text(w, what)) for w in raw]
    raise ComposePlanError(f"{what} must be a string or a list of strings.")


def _port(raw) -> int | None:
    """The container side of a ports/expose entry."""
    if isinstance(raw, dict):
        raw = raw.get("target")
    if isinstance(raw, int):
        port = raw
    else:
        text = str(raw or "").split("/")[0].strip()
        text = text.rsplit(":", 1)[-1]
        text = text.split("-")[0]
        if not text.isdigit():
            return None
        port = int(text)
    return port if 1 <= port <= 65535 else None


def _healthcheck(raw, service: str):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ComposePlanError(f"The healthcheck of {service} must be a mapping.")
    out = {}
    if raw.get("disable"):
        return {"disable": True}
    test = raw.get("test")
    if isinstance(test, str):
        out["test"] = ["CMD-SHELL", escape(_text(test, f"{service}: healthcheck"))]
    elif isinstance(test, list) and test and all(isinstance(t, str) for t in test) and len(test) <= 50:
        out["test"] = [escape(t) for t in test]
    elif test is not None:
        raise ComposePlanError(f"The healthcheck test of {service} must be a string or a list of strings.")
    for key in ("interval", "timeout", "start_period", "start_interval"):
        if key in raw:
            val = str(raw[key]).strip()
            if not _DURATION.match(val):
                raise ComposePlanError(f"{service}: healthcheck {key} must look like 10s or 1m.")
            out[key] = val
    if "retries" in raw:
        if not isinstance(raw["retries"], int) or not 0 <= raw["retries"] <= 100:
            raise ComposePlanError(f"{service}: healthcheck retries must be a number up to 100.")
        out["retries"] = raw["retries"]
    return out or None


def _depends_on(raw, service: str) -> dict:
    """{service: condition}; a plain list means service_started, as in Compose."""
    if raw is None:
        return {}
    if isinstance(raw, list):
        return {str(d): "service_started" for d in raw}
    if not isinstance(raw, dict):
        raise ComposePlanError(f"The depends_on of {service} must be a list or a mapping.")
    out = {}
    for dep, spec in raw.items():
        condition = (spec or {}).get("condition", "service_started") if isinstance(spec, dict) else "service_started"
        if condition not in DEPENDS_CONDITIONS:
            raise ComposePlanError(f"{service}: depends_on condition {str(condition)[:40]!r} is not one of "
                                   f"{', '.join(DEPENDS_CONDITIONS)}.")
        out[str(dep)] = condition
    return out


def _volumes(raw, service: str, warnings: list) -> list:
    """Named volumes only: [(volume name, absolute container path)]."""
    out = []
    for entry in raw or []:
        if isinstance(entry, dict):
            kind, source, target = entry.get("type", "volume"), str(entry.get("source") or ""), str(entry.get("target") or "")
        else:
            parts = _text(entry, f"A volume of {service}").split(":")
            source, target = (parts[0], parts[1]) if len(parts) >= 2 else ("", parts[0])
            kind = "volume" if source and _VOLUME.match(source) and not source.startswith((".", "~")) else "bind"
        if "docker.sock" in source or "docker.sock" in target:
            raise ComposePlanError(f"{service} mounts the Docker socket, which would give it control of the server. "
                                   "Codeskys never allows that.")
        if kind == "volume" and source and _VOLUME.match(source) and _ABS.match(target):
            out.append((source, target))
        elif kind in ("bind", "volume") and not source and _ABS.match(target):
            continue                           # anonymous volume: the container's own layer is enough
        else:
            warnings.append(f"{service}: the mount {source or '?'} -> {target or '?'} is not used (your files are "
                            "already inside the image; only named volumes are kept)")
    return out


# ---------------------------------------------------------------- planning

def plan(doc: dict, files: set, base: str = "") -> dict:
    """Check and normalise the compose document. ``files`` are the repository paths relative
    to ``base`` (the project root). Raises ComposePlanError."""
    warnings = []
    for key in doc:
        if key in ("configs", "secrets"):
            raise ComposePlanError(f"Top-level '{key}' read files from the server and are not supported. "
                                   "Use environment variables (Project → Settings) instead.")
        if key not in ("services", "volumes", "networks", "version", "name") and not str(key).startswith("x-"):
            warnings.append(f"the top-level '{key}' section is ignored")
    raw_services = {}
    for name, svc in doc["services"].items():
        if not isinstance(svc, dict):
            raise ComposePlanError(f"The service {name!r} is not a mapping.")
        if svc.get("profiles"):
            warnings.append(f"{name}: has profiles, so it does not start by default; skipped")
            continue
        raw_services[str(name)] = svc
    if not raw_services:
        raise ComposePlanError("The docker-compose file has no services that start by default.")
    if len(raw_services) > MAX_SERVICES:
        raise ComposePlanError(f"The docker-compose file has {len(raw_services)} services; Codeskys runs at most "
                               f"{MAX_SERVICES} per project.")
    services = []
    for name, svc in raw_services.items():
        if not _NAME.match(name):
            raise ComposePlanError(f"The service name {name[:40]!r} must use lowercase letters, digits, '-' or '_'.")
        unsafe = sorted(k for k in svc if k in UNSAFE_KEYS and svc[k] not in (None, False, [], {}))
        if unsafe:
            raise ComposePlanError(f"The service {name} asks for {', '.join(unsafe)}, which would give it access "
                                   "to the server. Codeskys runs every service as an ordinary, isolated container.")
        for k in svc:
            if k not in KEPT_KEYS and k not in IGNORED_KEYS and k not in UNSAFE_KEYS:
                warnings.append(f"{name}: '{k}' is ignored")
        entry = {"name": name, "environment": _env(svc.get("environment"), name),
                 "command": _words(svc.get("command"), f"The command of {name}"),
                 "entrypoint": _words(svc.get("entrypoint"), f"The entrypoint of {name}"),
                 "healthcheck": _healthcheck(svc.get("healthcheck"), name),
                 "volumes": _volumes(svc.get("volumes"), name, warnings),
                 "ports": [p for p in (_port(x) for x in (svc.get("ports") or [])) if p],
                 "expose": [p for p in (_port(x) for x in (svc.get("expose") or [])) if p]}
        for key in ("working_dir",):
            if svc.get(key) is not None:
                val = _text(svc[key], f"{name}: {key}")
                if not _ABS.match(val):
                    raise ComposePlanError(f"{name}: working_dir must be an absolute path inside the container.")
                entry[key] = val
        if svc.get("user") is not None:
            val = _text(svc["user"], f"{name}: user")
            if not re.match(r"^[A-Za-z0-9_.-]{1,32}(?::[A-Za-z0-9_.-]{1,32})?$", val):
                raise ComposePlanError(f"{name}: user must look like 1000 or 1000:1000 or node.")
            entry["user"] = val
        entry["depends_on"] = _depends_on(svc.get("depends_on"), name)
        build, image = svc.get("build"), svc.get("image")
        if build is not None:
            if isinstance(build, str):
                build = {"context": build}
            if not isinstance(build, dict):
                raise ComposePlanError(f"The build of {name} must be a path or a mapping.")
            ctx = str(build.get("context") or ".").strip()
            ctx = "" if ctx in (".", "./") else ctx.removeprefix("./").strip("/")
            if ctx and (not _PATH.match(ctx) or ".." in ctx.split("/")):
                raise ComposePlanError(f"The build context of {name} must be a folder inside the project.")
            dockerfile = str(build.get("dockerfile") or "Dockerfile").strip().removeprefix("./")
            if not _PATH.match(dockerfile) or ".." in dockerfile.split("/"):
                raise ComposePlanError(f"The Dockerfile of {name} must be a path inside its build context.")
            full = f"{ctx}/{dockerfile}" if ctx else dockerfile
            if files is not None and full not in files:
                raise ComposePlanError(f"The service {name} is built from {full}, which is not in the project.")
            if "dockerfile_inline" in build:
                raise ComposePlanError(f"{name}: an inline Dockerfile in the compose file is not supported; "
                                       "commit a Dockerfile instead.")
            args = build.get("args") or {}
            if isinstance(args, list):
                args = dict(a.partition("=")[::2] for a in args if isinstance(a, str))
            if not isinstance(args, dict) or len(args) > 50:
                raise ComposePlanError(f"The build args of {name} must be a mapping.")
            clean_args = {}
            for k, v in args.items():
                if not _ENV_KEY.match(str(k)):
                    raise ComposePlanError(f"{name}: build arg {str(k)[:40]!r} is not a valid name.")
                clean_args[str(k)] = escape(_text(v if v is not None else "", f"{name}: build arg {k}"))
            target = build.get("target")
            if target is not None and not re.match(r"^[A-Za-z0-9_.-]{1,64}$", str(target)):
                raise ComposePlanError(f"{name}: the build target must be a stage name.")
            entry.update(kind="build", context=ctx, dockerfile=dockerfile, path=full, args=clean_args,
                         target=str(target) if target is not None else None)
            if image and VULNERABLE.search(str(image)):
                raise _vulnerable(name, image)
        elif image is not None:
            repo, tag = image_repository(_text(image, f"The image of {name}"))
            if VULNERABLE.search(str(image)):
                raise _vulnerable(name, image)
            if repo not in DATA_IMAGES:
                hint = "add a Dockerfile for this service (build: ...) instead"
                own = _own_dockerfile(files, repo)
                if own:
                    hint = (f"your project has {own}, which looks like it builds this image: use "
                            f"build: {{ context: ., dockerfile: {own} }} for {name} instead of image:")
                raise ComposePlanError(
                    f"The service {name} uses the image {str(image)[:80]}. Codeskys runs the databases "
                    f"{', '.join(sorted(DATA_IMAGES))} from their official images, and services built from your own "
                    f"Dockerfiles; {hint}.")
            if tag and not _TAG.match(tag):
                raise ComposePlanError(f"The image tag of {name} is not valid.")
            entry.update(kind="data", image=f"{repo}:{tag or 'latest'}", repo=repo)
        else:
            raise ComposePlanError(f"The service {name} has neither build nor image.")
        services.append(entry)
    names = {s["name"] for s in services}
    for s in services:
        missing = [d for d in s["depends_on"] if d not in names]
        if missing:
            raise ComposePlanError(f"{s['name']} depends on {', '.join(missing)}, which is not a service here.")
    public = _public(services)
    if public is None:
        raise ComposePlanError("No service built from your code publishes a port, so there is no web page to show. "
                               "Add ports: [\"<port>\"] to the service that serves the website.")
    public_name = public["name"]
    if public_name != "app" and "app" in names:
        raise ComposePlanError("A service other than the public one is named 'app'; rename it (Codeskys uses 'app' "
                               "for the service that serves the website).")
    port = (public["ports"] or public["expose"])[0]
    volumes = sorted({v for s in services for v, _ in s["volumes"]})
    if len(volumes) > MAX_VOLUMES:
        raise ComposePlanError(f"The project uses {len(volumes)} volumes; at most {MAX_VOLUMES} are allowed.")
    total = 0
    for s in services:
        if s is public:
            s["limits"] = None                  # the project's own limits (engine fills them in)
        elif s["kind"] == "data":
            s["limits"] = DATA_IMAGES[s["repo"]]
        else:
            s["limits"] = WORKER_LIMITS
        total += (s["limits"] or (512, 0.5))[0]
    if total > MAX_TOTAL_MEMORY_MB:
        raise ComposePlanError(f"These services need about {total} MB of memory together; a Codeskys project may use "
                               f"{MAX_TOTAL_MEMORY_MB} MB. Remove a service or combine them.")
    # Other services that address the public one by its old name keep working.
    if public_name != "app":
        rx = re.compile(rf"(?<=[/@]){re.escape(public_name)}(?=[:/])|^{re.escape(public_name)}(?=:\d)")
        for s in services:
            s["environment"] = {k: rx.sub("app", v) for k, v in s["environment"].items()}
        for s in services:
            s["depends_on"] = {"app" if d == public_name else d: c for d, c in s["depends_on"].items()}
        public["name"] = "app"
        warnings.append(f"the public service {public_name} runs as 'app'")
    for s in services:
        if s["kind"] == "data":
            _need_db_password(s)
    return {"services": services, "port": port, "public": public_name, "volumes": volumes, "warnings": warnings,
            "required_env": required_env(services),
            "required_db_env": required_env([s for s in services if s["kind"] == "data"]),
            "db_credentials": db_credentials(services)}


# ${NAME} or ${NAME:?message}: no default, so the project must provide NAME ($$ is a literal $).
_REQUIRED = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]{0,127})(?::?\?[^}]*)?\}")
# What each official database image refuses to start without (any one of the names).
_DB_PASSWORD = {"postgres": ("POSTGRES_PASSWORD", "POSTGRES_HOST_AUTH_METHOD"),
                "mysql": ("MYSQL_ROOT_PASSWORD", "MYSQL_ALLOW_EMPTY_PASSWORD", "MYSQL_RANDOM_ROOT_PASSWORD"),
                "mariadb": ("MARIADB_ROOT_PASSWORD", "MYSQL_ROOT_PASSWORD", "MARIADB_ALLOW_EMPTY_ROOT_PASSWORD",
                            "MYSQL_ALLOW_EMPTY_PASSWORD", "MARIADB_RANDOM_ROOT_PASSWORD", "MYSQL_RANDOM_ROOT_PASSWORD")}


def required_env(services) -> list:
    """Project environment variables the compose file uses without a default."""
    names = set()
    for s in services:
        texts = list(s["environment"].values()) + list((s.get("args") or {}).values())
        for key in ("command", "entrypoint"):
            v = s.get(key)
            texts += [v] if isinstance(v, str) else (v or [])
        texts += (s.get("healthcheck") or {}).get("test", [])
        for text in texts:
            names.update(_REQUIRED.findall(text if isinstance(text, str) else ""))
    return sorted(names - {"PORT"})


def db_credentials(services) -> dict:
    """{variable: "password" | "user" | "database"} for the project variables that set up a database
    Codeskys runs here, judged by the database's own setting they feed (POSTGRES_PASSWORD: ${X} makes
    X a password, whatever X is called). Codeskys may generate exactly these; nothing else."""
    out = {}
    for s in services:
        if s["kind"] != "data":
            continue
        for key, value in s["environment"].items():
            k = key.upper()
            role = ("password" if "PASSWORD" in k else
                    "user" if k.endswith(("_USER", "_USERNAME")) else
                    "database" if k.endswith(("_DB", "_DATABASE")) else None)
            if role:
                for name in _REQUIRED.findall(value):
                    out.setdefault(name, role)
        words = s.get("command")
        for word in ([words] if isinstance(words, str) else words or []):
            # redis-server --requirepass ${REDIS_PASSWORD}
            for name in _REQUIRED.findall(word):
                if "PASS" in name.upper():
                    out.setdefault(name, "password")
    return out


def _need_db_password(s):
    accepted = _DB_PASSWORD.get(s["repo"])
    if accepted and not any(k in s["environment"] for k in accepted):
        raise ComposePlanError(
            f"The database service {s['name']} ({s['image']}) has no {accepted[0]}, so it would refuse to start. "
            f"Add {accepted[0]}: ${{DB_PASSWORD}} to its environment in the compose file and set DB_PASSWORD in "
            "Project → Settings → Environment variables (Codeskys never makes up passwords).")


def _own_dockerfile(files, repo: str) -> str:
    """A top-level Dockerfile.<x> whose <x> is part of the image name (Dockerfile.superset for
    pulseiq-superset), or ""."""
    for f in sorted(files or ()):
        if "/" not in f and f.startswith("Dockerfile.") and len(f) > 11:
            suffix = f[11:].lower()
            if suffix in repo.split("/")[-1].replace("_", "-").split("-"):
                return f
    return ""


def _public(services):
    candidates = [s for s in services if s["kind"] == "build" and s["ports"]]
    if not candidates:
        candidates = [s for s in services if s["kind"] == "build" and s["expose"]]
    if not candidates:
        return None
    for name in WEB_NAMES:
        for s in candidates:
            if s["name"] == name:
                return s
    return candidates[0]


def lab_refusal(what: str, joiner: str = ", ") -> ComposePlanError:
    """``what`` is e.g. "The service lab runs bkimminich/juice-shop" (or "The repository x/y" with " is ")."""
    return ComposePlanError(f"{what}{joiner}an intentionally vulnerable training application. Codeskys never puts those "
                            "on the internet; keep the lab on your own computer and publish your report or "
                            "Security Hub instead.")


def _vulnerable(name, image):
    return lab_refusal(f"The service {name} runs {str(image)[:80]}")


def find_compose(files: set) -> str | None:
    for name in COMPOSE_NAMES:
        if name in files:
            return name
    return None
