"""Coolify API gateway: the only process that holds the Coolify token.

The website talks to this gateway instead of Coolify. The gateway forwards only the
calls the Codeskys adapter makes (:class:`kova.engine.Coolify`), and for the ones that
can change what runs it checks the request first:

* a service may only be created in the configured project, server, environment and
  destination, never joined to the destination network, never deployed instantly;
* its compose file must pass :func:`kova.composegen.assert_safe_service` (one ``app``
  container, all capabilities dropped, no volumes, ports, networks, privileges, host
  namespaces or build options), so a leaked website cannot start a privileged
  container on the host through Coolify;
* its URL must be ``http://<slug>.<base domain>:<exposed port>`` for its own slug;
* environment variables must be plain, non-reserved names.

Whatever ``Authorization`` the caller sends is dropped and the token from
``COOLIFY_TOKEN_FILE`` is used. The token is never logged or returned.

Run inside the website image::

    python -m kova.coolifygw

Settings (environment): ``COOLIFY_UPSTREAM`` (``http://coolify:8080``),
``COOLIFY_TOKEN_FILE``, ``GW_PROJECT_UUID``, ``GW_SERVER_UUID``,
``GW_DESTINATION_UUID``, ``GW_ENVIRONMENT`` (``production``), ``GW_BASE_DOMAIN``
(``apps.codeskys.com``), ``GW_LISTEN`` (``0.0.0.0:8080``).
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .composegen import ComposeError, assert_safe_service, coolify_reserved

MAX_BODY = 1 << 20
_UUID = r"[a-z0-9]{8,64}"
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MARKER = re.compile(r"^Managed by Codeskys - project \d+ \(([a-z0-9-]+)\)\. Do not edit\.$")

# (method, path under /api/v1, allowed query parameters)
ROUTES = [
    ("GET", r"/version", set()),
    ("GET", r"/projects", set()),
    ("GET", r"/servers", set()),
    ("GET", r"/security/keys", set()),
    ("GET", r"/services", set()),
    ("POST", r"/services", set()),
    ("GET", rf"/services/{_UUID}", set()),
    ("PATCH", rf"/services/{_UUID}", set()),
    ("DELETE", rf"/services/{_UUID}", {"delete_configurations", "delete_volumes", "docker_cleanup",
                                        "delete_connected_networks"}),
    ("PATCH", rf"/services/{_UUID}/envs/bulk", set()),
    ("GET", rf"/services/{_UUID}/logs", {"sub_service_name", "lines"}),
    ("POST", rf"/services/{_UUID}/start", set()),
    ("POST", rf"/services/{_UUID}/stop", {"docker_cleanup"}),
    ("POST", rf"/services/{_UUID}/restart", set()),
]
_ROUTES = [(m, re.compile(rf"^{p}$"), q) for m, p, q in ROUTES]
_SERVICE_PATCH = {"description", "docker_compose_raw", "connect_to_docker_network", "urls"}
_SERVICE_CREATE = _SERVICE_PATCH | {"project_uuid", "server_uuid", "environment_name", "name",
                                    "instant_deploy", "destination_uuid"}
_ENV_ITEM = {"key", "value", "is_literal"}


class Refused(ValueError):
    """The request is not one Codeskys makes; it is not forwarded."""


def settings(env=os.environ) -> dict:
    return {
        "upstream": env.get("COOLIFY_UPSTREAM", "http://coolify:8080").rstrip("/"),
        "token_file": env.get("COOLIFY_TOKEN_FILE", "/run/secrets/coolify_token"),
        "project_uuid": env.get("GW_PROJECT_UUID", ""),
        "server_uuid": env.get("GW_SERVER_UUID", ""),
        "destination_uuid": env.get("GW_DESTINATION_UUID", ""),
        "environment_name": env.get("GW_ENVIRONMENT", "production"),
        "base_domain": env.get("GW_BASE_DOMAIN", "apps.codeskys.com").strip(".").lower(),
        "listen": env.get("GW_LISTEN", "0.0.0.0:8080"),
    }


def _need(cond, why):
    if not cond:
        raise Refused(why)


def route(method: str, path: str, query: dict):
    """The matching route, or Refused. ``path`` is below /api/v1."""
    for m, rx, allowed in _ROUTES:
        if m == method and rx.match(path):
            _need(set(query) <= allowed, "query parameters not allowed")
            return m, rx.pattern
    raise Refused("endpoint not allowed")


def _compose(raw) -> dict:
    _need(isinstance(raw, str) and len(raw) <= MAX_BODY, "docker_compose_raw")
    try:
        doc = json.loads(base64.b64decode(raw, validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise Refused("docker_compose_raw is not a base64 Codeskys compose file") from None
    try:
        return assert_safe_service(doc)
    except (ComposeError, KeyError, TypeError) as exc:
        raise Refused(str(exc)) from None


def check_service(body, cfg: dict, create: bool) -> None:
    """Refused unless ``body`` is a service create/update the adapter would send."""
    _need(isinstance(body, dict) and body, "body must be an object")
    _need(set(body) <= (_SERVICE_CREATE if create else _SERVICE_PATCH), "unexpected fields")
    slug = None
    if create:
        for key in ("project_uuid", "server_uuid", "destination_uuid", "environment_name"):
            _need(cfg[key] and body.get(key) == cfg[key], f"{key} must be the Codeskys one")
        _need(isinstance(body.get("name"), str) and _SLUG.match(body["name"]), "name")
        _need(body.get("instant_deploy") is False, "instant_deploy must be false")
        _need("docker_compose_raw" in body and "urls" in body, "compose file and URL are required")
        slug = body["name"]
    if create or "connect_to_docker_network" in body:
        _need(body.get("connect_to_docker_network") is False, "connect_to_docker_network must be false")
    if "description" in body:
        m = _MARKER.match(body["description"]) if isinstance(body["description"], str) else None
        _need(m and (slug is None or m.group(1) == slug), "description must be the Codeskys ownership marker")
        slug = slug or m.group(1)
    doc = _compose(body["docker_compose_raw"]) if "docker_compose_raw" in body else None
    if "urls" in body:
        _need(doc is not None, "a URL change needs the compose file")
        urls = body["urls"]
        _need(isinstance(urls, list) and len(urls) == 1 and isinstance(urls[0], dict)
              and set(urls[0]) == {"name", "url"} and urls[0]["name"] == "app", "urls")
        rx = rf"^http://([a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?)\.{re.escape(cfg['base_domain'])}:(\d{{1,5}})$"
        m = re.match(rx, urls[0]["url"]) if isinstance(urls[0]["url"], str) else None
        _need(m, f"the URL must be http://<slug>.{cfg['base_domain']}:<port>")
        _need(m.group(2) == str(doc["services"]["app"]["expose"][0]), "the URL port must be the exposed port")
        label = (doc["services"]["app"].get("labels") or {}).get("codeskys.project")
        _need(slug is None or m.group(1) == slug, "the URL must use the project's own slug")
        _need(label is None or m.group(1) == label, "the URL must match the compose file's project")


def check_envs(body) -> None:
    _need(isinstance(body, dict) and set(body) == {"data"} and isinstance(body["data"], list)
          and len(body["data"]) <= 200, "body must be {data: [...]}")
    for item in body["data"]:
        _need(isinstance(item, dict) and {"key", "value"} <= set(item) <= _ENV_ITEM, "environment item")
        _need(isinstance(item["key"], str) and _ENV_KEY.match(item["key"])
              and not coolify_reserved(item["key"]), "environment variable name")
        _need(isinstance(item["value"], str) and len(item["value"]) <= 32768, "environment variable value")
        _need(item.get("is_literal", True) is True, "environment values are literal")


def check(method: str, path: str, query: dict, body, cfg: dict) -> None:
    route(method, path, query)
    if method == "POST" and path == "/services":
        check_service(body, cfg, create=True)
    elif method == "PATCH" and path.endswith("/envs/bulk"):
        check_envs(body)
    elif method == "PATCH":
        check_service(body, cfg, create=False)
    else:
        _need(body is None, "this endpoint takes no body")


def _token(cfg) -> str:
    with open(cfg["token_file"], encoding="utf-8") as fh:
        token = fh.read().strip()
    if not token:
        raise RuntimeError("empty Coolify token file")
    return token


class Handler(BaseHTTPRequestHandler):
    cfg: dict = {}
    server_version = "codeskys-coolifygw"
    sys_version = ""

    def _reply(self, status: int, payload: bytes, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _refuse(self, status: int, why: str):
        self._reply(status, json.dumps({"message": f"Refused by the Codeskys gateway: {why}"}).encode())

    def _handle(self):
        parts = urllib.parse.urlsplit(self.path)
        if not parts.path.startswith("/api/v1/"):
            return self._refuse(404, "endpoint not allowed")
        path = parts.path[len("/api/v1"):]
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._refuse(413, "body too large")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            return self._refuse(400, "body is not JSON")
        try:
            check(self.command, path, query, body, self.cfg)
        except Refused as exc:
            self.log_message("REFUSED %s %s: %s", self.command, parts.path, exc)
            return self._refuse(403, str(exc))
        req = urllib.request.Request(
            self.cfg["upstream"] + parts.path + (f"?{parts.query}" if parts.query else ""),
            data=raw or None, method=self.command,
            headers={"Authorization": f"Bearer {_token(self.cfg)}", "Accept": "application/json",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, payload, ctype = resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as exc:
            status, payload, ctype = exc.code, exc.read(), exc.headers.get("Content-Type", "application/json")
        except (urllib.error.URLError, TimeoutError, OSError):
            return self._refuse(502, "the deployment engine did not answer")
        self.log_message("%s %s -> %s", self.command, parts.path, status)
        self._reply(status, payload, ctype)

    do_GET = do_POST = do_PATCH = do_DELETE = _handle

    def do_PUT(self):
        self._refuse(403, "endpoint not allowed")

    def log_message(self, fmt, *args):   # no query strings, bodies or headers
        sys.stderr.write("coolifygw " + (fmt % args) + "\n")


def main() -> None:
    cfg = settings()
    missing = [k for k in ("project_uuid", "server_uuid", "destination_uuid") if not cfg[k]]
    if missing:
        sys.exit(f"coolifygw: missing settings {missing}")
    _token(cfg)                                   # fail at start, not on the first deploy
    Handler.cfg = cfg
    host, port = cfg["listen"].rsplit(":", 1)
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()


if __name__ == "__main__":
    main()
