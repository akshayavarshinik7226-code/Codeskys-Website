"""Generate the Docker Compose file for one Codeskys deployment.

A real Codeskys deployment is two containers:

``app``               the student's application, built from their repository at
                      the exact commit. The repository is never modified: no
                      file is added to it, nothing is pushed.
``codeskys-runtime``  the runtime-proxy sidecar (ForgeOps ``runtime/`` image).
                      It alone holds the deployment's runtime secret (a
                      read-only file) and exchanges it with authz for
                      short-lived tokens to call the private Data API.

Networks (the security property this file exists to guarantee)::

    ingress  (external, e.g. codeskys-apps)   app <-> Traefik; internet egress
    private  (per deployment, internal)       app <-> codeskys-runtime ONLY
    link     (external, internal)             codeskys-runtime <-> authz, dataapi

* The app is never on ``link``: it has no route to authz or the Data API.
* The sidecar is never on ``ingress``: another student's app (also on ingress)
  cannot reach this deployment's sidecar and borrow its credential.
* Neither is ever on ``platform`` (postgres) or ``control`` (website <-> authz).

The output is JSON, which is valid YAML, built from data structures: no user
input is ever spliced into template text. Every value that reaches the file is
validated first.
"""
from __future__ import annotations

import ipaddress
import json
import re

from .gitinfo import FRAMEWORKS
from .runtime_secrets import PLATFORM_SECRET_GIDS

_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DEP = re.compile(r"^dep_[A-Za-z0-9]{6,28}$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_GITHUB = re.compile(r"^https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,199}$")
_NETWORK = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
_MEMORY = re.compile(r"^\d{1,6}[kmgKMG]$")
_CPUS = re.compile(r"^\d{1,3}(\.\d{1,2})?$")
_PATH = re.compile(r"^/[A-Za-z0-9._/-]{1,250}$")

# Platform-controlled names the student's env vars may not set on the app.
RESERVED = {"CODESKYS_DEPLOYMENT_ID", "CODESKYS_REPOSITORY_ID", "CODESKYS_RUNTIME_URL",
            "CODESKYS_RUNTIME_SECRET", "CODESKYS_RUNTIME_SECRET_FILE", "AUTHZ_INTERNAL_URL",
            "DATA_API_URL", "PORT"}

SIDECAR_PORT = 8090
SIDECAR_ALIAS = "codeskys-runtime"
# Each project's private network gets its own /28 from this pool. The sidecar only
# serves clients inside it (CODESKYS_RUNTIME_CLIENT_CIDRS), so a container on the
# shared link network can never use another deployment's sidecar.
DEFAULT_SUBNET_POOL = "10.213.0.0/16"


def private_subnet(project_id: int, pool: str = DEFAULT_SUBNET_POOL) -> str:
    """Deterministic, collision-free /28 for a project (one per project id)."""
    net = ipaddress.ip_network(pool)
    if net.version != 4 or not net.is_private or net.prefixlen > 24:
        raise ComposeError("runtime subnet pool must be a private IPv4 /24 or larger")
    count = 2 ** (28 - net.prefixlen)
    if not isinstance(project_id, int) or not 1 <= project_id < count:
        raise ComposeError("project id outside the runtime subnet pool")
    return str(ipaddress.ip_network((int(net.network_address) + project_id * 16, 28)))


class ComposeError(ValueError):
    """Input that must not reach a compose file."""


def _check(regex, value, what):
    s = "" if value is None else str(value)
    if not regex.match(s):
        raise ComposeError(f"invalid {what}")
    return s


_SUBDIR = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


def build_context(repo_url: str, commit_sha: str, subdir: str = "") -> str:
    """Docker git build context pinned to an exact commit (public repositories),
    optionally a sub-directory of it (the project's root directory)."""
    m = _GITHUB.match((repo_url or "").rstrip("/").removesuffix(".git"))
    if not m:
        raise ComposeError("only https://github.com/<owner>/<repo> repositories are supported")
    _check(_SHA, commit_sha, "commit sha (40 hex characters)")
    subdir = (subdir or "").strip().strip("/")
    if subdir and (not _SUBDIR.match(subdir) or ".." in subdir.split("/")):
        raise ComposeError("invalid root directory")
    return f"https://github.com/{m.group(1)}/{m.group(2)}.git#{commit_sha}" + (f":{subdir}" if subdir else "")


def generate(*, slug: str, deployment_id: str, repository_id: int, app_context: str,
             runtime_image: str, secret_file: str, ingress_network: str, link_network: str,
             port: int = 3000, dockerfile: str = "Dockerfile", dockerfile_inline: str | None = None,
             env_keys=(),
             memory: str = "512M", cpus: str = "0.5", runtime_memory: str = "128M",
             runtime_cpus: str = "0.25", runtime_gid: int | None = None, private_cidr: str = "",
             project_id: int | None = None, website_instance: str = "default",
             authz_url: str = "http://authz:8010", data_api_url: str = "http://dataapi:8020") -> dict:
    """Return the compose document (a dict). ``json.dumps`` it to write the file.

    ``app_context`` is a pinned git context from :func:`build_context` in
    production, or an absolute local directory in the local test harness.
    ``secret_file`` is the HOST path of this deployment's runtime-secret file.
    ``env_keys`` are the names of the project's variables; their VALUES are
    supplied by the engine at run time (``${KEY}``), never written here.
    """
    slug = _check(_SLUG, slug, "project slug")
    deployment_id = _check(_DEP, deployment_id, "deployment id")
    if not isinstance(repository_id, int) or repository_id < 1:
        raise ComposeError("invalid repository id")
    if not (app_context.startswith("https://github.com/") and "#" in app_context):
        _check(_PATH, app_context.replace("\\", "/") if not re.match(r"^[A-Za-z]:", app_context) else "/x",
               "local build context")
    _check(_IMAGE, runtime_image, "runtime image")
    if not (secret_file.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", secret_file)):
        raise ComposeError("secret file must be an absolute host path")
    if deployment_id not in secret_file.replace("\\", "/").split("/"):
        raise ComposeError("secret file must belong to this deployment")
    for net, what in ((ingress_network, "ingress network"), (link_network, "link network")):
        _check(_NETWORK, net, what)
        if _reserved_network(net):
            raise ComposeError(f"{what} may not be a Docker built-in or platform network")
    if ingress_network == link_network:
        raise ComposeError("ingress and link networks must differ")
    if not isinstance(port, int) or not 1 <= port <= 65535 or port == SIDECAR_PORT:
        raise ComposeError("invalid app port")
    if not re.match(r"^[A-Za-z0-9._/-]{1,120}$", dockerfile) or ".." in dockerfile:
        raise ComposeError("invalid Dockerfile path")
    for value, regex, what in ((memory, _MEMORY, "memory limit"), (cpus, _CPUS, "CPU limit"),
                               (runtime_memory, _MEMORY, "sidecar memory"), (runtime_cpus, _CPUS, "sidecar CPUs")):
        _check(regex, value, what)

    keys = []
    for k in env_keys:
        k = _check(_ENV_KEY, k, "environment variable name")
        if k not in RESERVED and k not in keys:
            keys.append(k)

    try:
        cidr = ipaddress.ip_network(private_cidr)
    except ValueError:
        raise ComposeError("invalid private subnet") from None
    if cidr.version != 4 or not cidr.is_private or cidr.prefixlen < 24:
        raise ComposeError("private subnet must be a small private IPv4 network")
    _check(re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$"), website_instance, "website instance name")
    private = f"cs-{slug}-private"
    hardening = {"cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "pids_limit": 256,
                 "restart": "unless-stopped", "init": True,
                 "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}}

    app_env = {k: "${" + k + "}" for k in keys}
    app_env.update({
        "PORT": str(port),
        "CODESKYS_RUNTIME_URL": f"http://{SIDECAR_ALIAS}:{SIDECAR_PORT}",
        "CODESKYS_DEPLOYMENT_ID": deployment_id,
        "CODESKYS_REPOSITORY_ID": str(repository_id),
    })
    app = {
        "build": ({"context": app_context, "dockerfile_inline": compose_escape(dockerfile_inline)} if dockerfile_inline
                  else {"context": app_context, "dockerfile": dockerfile}),
        "environment": app_env,
        "expose": [str(port)],
        "networks": {"ingress": {}, "private": {}},
        "mem_limit": memory, "cpus": float(cpus),
        "labels": {"codeskys.deployment": deployment_id, "codeskys.role": "app",
                   "codeskys.project": slug},
        "depends_on": {"codeskys-runtime": {"condition": "service_started"}},
        **hardening,
    }
    runtime = {
        "image": runtime_image,
        "environment": {
            "CODESKYS_DEPLOYMENT_ID": deployment_id,
            "CODESKYS_REPOSITORY_ID": str(repository_id),
            "CODESKYS_RUNTIME_SECRET_FILE": "/run/secrets/codeskys_runtime",
            "AUTHZ_INTERNAL_URL": authz_url,
            "DATA_API_URL": data_api_url,
            # Only the app on this deployment's private network may use the sidecar.
            "CODESKYS_RUNTIME_CLIENT_CIDRS": str(cidr),
        },
        "volumes": [{"type": "bind", "source": secret_file, "target": "/run/secrets/codeskys_runtime",
                     "read_only": True}],
        "networks": {"private": {"aliases": [SIDECAR_ALIAS]}, "link": {}},
        "read_only": True,
        "tmpfs": ["/tmp"],
        "mem_limit": runtime_memory, "cpus": float(runtime_cpus),
        "labels": {"codeskys.deployment": deployment_id, "codeskys.role": "runtime",
                   "codeskys.project": slug},
        **hardening,
    }
    if runtime_gid is not None:
        if not isinstance(runtime_gid, int) or not 1 <= runtime_gid <= 65535:
            raise ComposeError("invalid runtime group id")
        if runtime_gid in PLATFORM_SECRET_GIDS:
            raise ComposeError("runtime group id is reserved for platform secrets")
        runtime["group_add"] = [str(runtime_gid)]

    return assert_safe({
        "name": f"cs-{slug}",
        "services": {"app": app, "codeskys-runtime": runtime},
        "networks": {
            "private": {"name": private, "internal": True,
                        "ipam": {"config": [{"subnet": str(cidr)}]},
                        # Ownership labels. Docker refuses an overlapping explicit subnet, and
                        # Compose refuses to adopt a network created by another project, so a
                        # clash (restored kova.db reusing ids, two website instances on one
                        # host, a foreign network in the pool) FAILS the deploy. It never
                        # silently shares a network. Never retry with a different subnet.
                        "labels": {"codeskys.deployment": deployment_id,
                                   "codeskys.project_id": str(project_id or ""),
                                   "codeskys.website_instance": website_instance}},
            "ingress": {"name": ingress_network, "external": True},
            "link": {"name": link_network, "external": True},
        },
    })


# Docker built-ins attach a container to the host's network stack (host) or to the
# default bridge; codeskys_* are the production stack's networks (platform holds
# postgres), coolify holds Coolify's database and API.
_BUILTIN_NETWORKS = {"host", "bridge", "none", "default", "coolify"}


def _reserved_network(name: str) -> bool:
    return name.lower() in _BUILTIN_NETWORKS or name.lower().startswith("codeskys_")


# Exactly the keys generate() writes. Anything else (privileged, network_mode, pid,
# ipc, cap_add, devices, extra_hosts, sysctls, userns_mode, volumes on the app, and
# in build: network, entitlements, privileged, ssh, secrets, extra_hosts, args, ...)
# could undo the isolation, so it is refused rather than filtered.
_TOP_KEYS = {"name", "services", "networks"}
_COMMON_KEYS = {"environment", "networks", "mem_limit", "cpus", "labels", "cap_drop", "security_opt",
                "pids_limit", "restart", "init", "logging"}
_SERVICE_KEYS = {"app": _COMMON_KEYS | {"build", "expose", "depends_on"},
                 "codeskys-runtime": _COMMON_KEYS | {"image", "volumes", "read_only", "tmpfs", "group_add"}}
_BUILD_KEYS = {"context", "dockerfile"}
_INLINE_BUILD_KEYS = {"context", "dockerfile_inline"}
# The only instructions dockerfiles.generate() writes; RUN and CMD only in exec (JSON) form.
_INLINE_LINE = re.compile(r'^(FROM \S+( AS build)?|WORKDIR /app'
                          r'|COPY (--chown=(node:node|10001:10001) )?\. \.|COPY --from=build /site /usr/share/nginx/html'
                          r'|COPY [A-Za-z0-9._/-]+/ /usr/share/nginx/html/|ENV [A-Z_]+=\S+( [A-Z_]+=\S+)*'
                          r'|USER (node|root|101|10001)|EXPOSE \d{1,5}|RUN \[.*\]|CMD \[.*\])$')


def compose_escape(text: str) -> str:
    """Compose interpolates ``$VAR`` everywhere, dockerfile_inline included: that would
    blank shell variables in the build and could pull Compose's environment into it.
    ``$$`` is Compose's literal dollar."""
    return text.replace("$", "$$")


def _inline_problem(stored) -> str:
    """Why a compose-stored (escaped) inline Dockerfile is not one dockerfiles.generate()
    could have written ("" if it is)."""
    from . import buildpolicy, dockerfiles      # lazy: both import this module
    if not isinstance(stored, str) or len(stored) > 16384:
        return "missing or too large"
    if "$" in stored.replace("$$", ""):
        return "unescaped $ (Compose would interpolate it)"
    text = stored.replace("$$", "$")
    lines = text.splitlines()
    if not lines or not lines[0].startswith(dockerfiles.MARKER):
        return "not generated by Codeskys"
    for line in lines[1:]:
        if not _INLINE_LINE.match(line):
            return f"unexpected instruction: {line[:60]}"
        if line.startswith("FROM ") and line.split()[1] not in dockerfiles.BASE_IMAGES:
            return f"base image {line.split()[1]} is not approved"
        if line.startswith(("RUN ", "CMD ")):
            try:
                args = json.loads(line[4:])
            except ValueError:
                return "RUN/CMD must be in exec form"
            if not (isinstance(args, list) and len(args) == 3 and args[:2] == ["/bin/sh", "-c"]
                    and isinstance(args[2], str)):
                return "RUN/CMD must be [\"/bin/sh\", \"-c\", <command>]"
    problems = buildpolicy.dockerfile_problems(text)
    return problems[0] if problems else ""
_NETWORK_KEYS = {"private": {"name", "internal", "ipam", "labels"},
                 "ingress": {"name", "external"}, "link": {"name", "external"}}


def assert_safe(doc: dict) -> dict:
    """Refuse any compose document that is not exactly the shape generate() writes.

    Used on our own output and on what Coolify reports it stored (buildpolicy):
    the build must never get host networking, entitlements or privileges, and no
    container may get host namespaces, devices, capabilities or extra mounts.
    """
    def need(cond, what):
        if not cond:
            raise ComposeError(f"unsafe compose file: {what}")

    need(isinstance(doc, dict) and set(doc) <= _TOP_KEYS, "unexpected top-level keys")
    services, networks = doc.get("services"), doc.get("networks")
    need(isinstance(services, dict) and set(services) == set(_SERVICE_KEYS), "unexpected services")
    need(isinstance(networks, dict) and set(networks) == set(_NETWORK_KEYS), "unexpected networks")
    for name, keys in _NETWORK_KEYS.items():
        net = networks[name]
        need(isinstance(net, dict) and set(net) <= keys, f"unexpected keys on network {name}")
        need(isinstance(net.get("name"), str) and _NETWORK.match(net["name"])
             and not _reserved_network(net["name"]), f"network {name} has a reserved or invalid name")
    need(networks["private"].get("internal") is True, "the private network must be internal")
    need(networks["ingress"].get("external") is True and networks["link"].get("external") is True,
         "ingress and link must be existing networks")
    for name, keys in _SERVICE_KEYS.items():
        svc = services[name]
        need(isinstance(svc, dict) and set(svc) <= keys, f"unexpected keys on service {name}")
        need(svc.get("cap_drop") == ["ALL"], f"{name} must drop all capabilities")
        need(svc.get("security_opt") == ["no-new-privileges:true"], f"{name} security options")
        need(isinstance(svc.get("networks"), dict) and set(svc["networks"]) <= set(_NETWORK_KEYS),
             f"{name} joins an undeclared network")
    app, runtime = services["app"], services["codeskys-runtime"]
    build = app.get("build")
    need(isinstance(build, dict) and set(build) in (_BUILD_KEYS, _INLINE_BUILD_KEYS),
         "the app build may only set context and dockerfile (or a Codeskys-generated dockerfile_inline)")
    if "dockerfile_inline" in build:
        problem = _inline_problem(build["dockerfile_inline"])
        need(not problem, f"inline Dockerfile: {problem}")
    need(set(app["networks"]) == {"ingress", "private"}, "app networks")
    need(set(runtime["networks"]) == {"private", "link"}, "sidecar networks")
    vols = runtime.get("volumes")
    need(isinstance(vols, list) and len(vols) == 1 and isinstance(vols[0], dict)
         and vols[0].get("type") == "bind" and vols[0].get("read_only") is True
         and vols[0].get("target") == "/run/secrets/codeskys_runtime"
         and set(vols[0]) == {"type", "source", "target", "read_only"},
         "the sidecar may only mount its own secret file, read-only")
    for gid in runtime.get("group_add", []):
        need(str(gid).isdigit() and int(gid) not in PLATFORM_SECRET_GIDS, "sidecar group")
    return doc


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


# ---------------------------------------------------------------- Coolify Services API (4.7.5)
#
# Coolify's serviceParser (bootstrap/helpers/parsers.php, v4.7.5) attaches EVERY
# top-level network to EVERY container of a service (only network_mode opts out),
# then adds the service's own network (named after its uuid) and connects
# coolify-proxy to it. The app/sidecar split above (app never on ``link``) cannot
# survive that, and Coolify never removes networks a compose file creates. So a
# Coolify service runs the student's app ALONE, with no networks of its own: it ends
# up only on the service's network, shared with coolify-proxy and nothing else.

# Names Coolify treats as "magic" (generated URLs, passwords) or sets itself.
_COOLIFY_MAGIC = ("SERVICE_", "COOLIFY_")
_SERVICE_APP_KEYS = {"build", "environment", "expose", "mem_limit", "cpus", "labels", "cap_drop",
                     "security_opt", "pids_limit", "restart", "init", "logging"}


def coolify_reserved(key: str) -> bool:
    """A student variable Codeskys does not pass to a Coolify service."""
    return key in RESERVED or key.upper().startswith(_COOLIFY_MAGIC)


def generate_service(*, slug: str, deployment: int, app_context: str, port: int = 3000,
                     dockerfile: str = "Dockerfile", dockerfile_inline: str | None = None, env_keys=(),
                     memory: str = "512M", cpus: str = "0.5") -> dict:
    """The compose document for one Coolify service: the ``app`` container only."""
    slug = _check(_SLUG, slug, "project slug")
    if not isinstance(deployment, int) or deployment < 1:
        raise ComposeError("invalid deployment number")
    if not (app_context.startswith("https://github.com/") and "#" in app_context):
        raise ComposeError("the build context must be a pinned GitHub repository")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ComposeError("invalid app port")
    if not re.match(r"^[A-Za-z0-9._/-]{1,120}$", dockerfile) or ".." in dockerfile:
        raise ComposeError("invalid Dockerfile path")
    for value, regex, what in ((memory, _MEMORY, "memory limit"), (cpus, _CPUS, "CPU limit")):
        _check(regex, value, what)
    env = {}
    for k in env_keys:
        k = _check(_ENV_KEY, k, "environment variable name")
        if not coolify_reserved(k):
            env[k] = "${" + k + "}"
    env["PORT"] = str(port)
    return assert_safe_service({"services": {"app": {
        "build": ({"context": app_context, "dockerfile_inline": compose_escape(dockerfile_inline)}
                  if dockerfile_inline else {"context": app_context, "dockerfile": dockerfile}),
        "environment": env,
        "expose": [str(port)],
        "mem_limit": memory, "cpus": float(cpus),
        "labels": {"codeskys.deployment": str(deployment), "codeskys.role": "app", "codeskys.project": slug},
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "pids_limit": 256,
        "restart": "unless-stopped", "init": True,
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }}})


def assert_safe_service(doc: dict) -> dict:
    """Refuse any compose document that is not exactly the shape generate_service() writes:
    no networks, volumes, ports, privileges, host namespaces or build options."""
    def need(cond, what):
        if not cond:
            raise ComposeError(f"unsafe compose file: {what}")

    need(isinstance(doc, dict) and set(doc) == {"services"}, "unexpected top-level keys")
    services = doc["services"]
    need(isinstance(services, dict) and set(services) == {"app"}, "unexpected services")
    app = services["app"]
    need(isinstance(app, dict) and set(app) <= _SERVICE_APP_KEYS, "unexpected keys on service app")
    need(app.get("cap_drop") == ["ALL"], "app must drop all capabilities")
    need(app.get("security_opt") == ["no-new-privileges:true"], "app security options")
    build = app.get("build")
    need(isinstance(build, dict) and set(build) in (_BUILD_KEYS, _INLINE_BUILD_KEYS),
         "the app build may only set context and dockerfile (or a Codeskys-generated dockerfile_inline)")
    need(isinstance(build.get("context"), str) and build["context"].startswith("https://github.com/"),
         "the build context must be a GitHub repository")
    if "dockerfile_inline" in build:
        problem = _inline_problem(build["dockerfile_inline"])
        need(not problem, f"inline Dockerfile: {problem}")
    env = app.get("environment")
    need(isinstance(env, dict) and all(isinstance(k, str) and _ENV_KEY.match(k) and isinstance(v, str)
                                       for k, v in env.items()), "environment")
    need(not any(k != "PORT" and coolify_reserved(k) for k in env), "reserved environment variable")
    need(isinstance(app.get("expose"), list) and len(app["expose"]) == 1 and str(app["expose"][0]).isdigit(),
         "the app exposes exactly one port")
    return doc


def for_service(project, dep, *, env_keys=()) -> dict:
    """Engine wrapper: project/deployment rows -> the Coolify service compose document."""
    from . import dockerfiles
    fw = FRAMEWORKS.get(project["framework"], FRAMEWORKS["static"])
    context = build_context(project["repo_url"], dep["commit_sha"], project["root_dir"] or "")
    port = int(project["port"] or fw.get("port") or 3000)
    inline = None
    if project["framework"] != "docker":
        try:
            inline, port = dockerfiles.generate(project)
        except dockerfiles.DockerfileError as exc:
            raise ComposeError(str(exc)) from None
    return generate_service(slug=project["slug"], deployment=int(dep["id"]), app_context=context, port=port,
                            dockerfile_inline=inline, env_keys=env_keys,
                            memory=project["limits_memory"] or "512M", cpus=project["limits_cpus"] or "0.5")


def service_port(doc: dict) -> int:
    return int(doc["services"]["app"]["expose"][0])


def for_project(project, dep, *, app_config, secret_host_file: str, env_keys=(),
                app_context: str | None = None) -> dict:
    """Convenience wrapper used by the engine: project/deployment rows -> compose."""
    from . import dockerfiles
    fw = FRAMEWORKS.get(project["framework"], FRAMEWORKS["static"])
    context = app_context or build_context(project["repo_url"], dep["commit_sha"], project["root_dir"] or "")
    port = int(project["port"] or fw.get("port") or 3000)
    inline = None
    if project["framework"] != "docker":
        try:
            inline, port = dockerfiles.generate(project)
        except dockerfiles.DockerfileError as exc:
            raise ComposeError(str(exc)) from None
    gid = app_config.get("RUNTIME_SECRET_GID")
    return generate(
        dockerfile_inline=inline,
        slug=project["slug"], deployment_id=dep["authz_deployment_id"],
        repository_id=int(project["github_repo_id"]), app_context=context,
        runtime_image=app_config.get("RUNTIME_IMAGE") or "codeskys/runtime:local",
        secret_file=secret_host_file,
        ingress_network=app_config.get("APPS_NETWORK") or "codeskys-apps",
        link_network=app_config.get("RUNTIME_LINK_NETWORK") or "codeskys-runtime-link",
        port=port, env_keys=env_keys, memory=project["limits_memory"] or "512M",
        cpus=project["limits_cpus"] or "0.5", runtime_gid=int(gid) if gid else None,
        private_cidr=private_subnet(int(project["id"]), app_config.get("RUNTIME_SUBNET_POOL") or DEFAULT_SUBNET_POOL),
        project_id=int(project["id"]), website_instance=app_config.get("WEBSITE_INSTANCE") or "default",
        authz_url=app_config.get("RUNTIME_AUTHZ_URL") or "http://authz:8010",
        data_api_url=app_config.get("RUNTIME_DATA_API_URL") or "http://dataapi:8020",
    )
