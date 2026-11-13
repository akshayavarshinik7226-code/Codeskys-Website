"""Real Coolify API integration, driven against a mocked Coolify 4.7.5 Services API.

The fake enforces what Coolify 4.7.5 itself checks (ServicesController.php at the
v4.7.5 tag): allowed fields, the description/name character sets, base64 compose,
URLs naming an existing container, "already running"/"already stopped" refusals,
compose files hidden from tokens without read:sensitive. These prove the request
contract (method, path, body, guards). They are NOT evidence that a deployment works
against a live Coolify server.
"""
import base64
import json
import re
import shutil
import tempfile
from unittest import mock

from kova import composegen, engine, jobs
from kova.db import connect, set_setting

from .base import ADMIN
from .test_platform import PlatformCase

# ValidationPatterns.php (v4.7.5): \p{L}\p{M}\p{N} approximated by \w.
DESCRIPTION_PATTERN = re.compile(r"^[\w\s\-_.,!?()'\"+=*@/&]+$")
NAME_PATTERN = re.compile(r"^[\w\s\-_.@/&()#,:+]+$")
CREATE_FIELDS = {"name", "description", "project_uuid", "environment_name", "environment_uuid", "server_uuid",
                 "destination_uuid", "instant_deploy", "docker_compose_raw", "connect_to_docker_network", "urls",
                 "force_domain_override", "is_container_label_escape_enabled", "redact_all_env_values_in_logs",
                 "tags"}
UPDATE_FIELDS = {"name", "description", "instant_deploy", "docker_compose_raw", "connect_to_docker_network", "urls",
                 "force_domain_override", "is_container_label_escape_enabled", "redact_all_env_values_in_logs",
                 "include_coolify_default_variables"}
ENV_FIELDS = {"key", "value", "is_literal", "is_multiline", "is_shown_once", "comment"}


class FakeCoolify:
    """Records calls and answers like the Coolify 4.7.5 Services API."""

    def __init__(self, app_name=None, app_statuses=None, sensitive=False, scoped=False):
        self.calls = []
        self.authz = None                  # FakeAuthz, created on the first authz call
        self.services = {}                 # uuid -> stored state
        self.next_uuid = 123
        self.app_name = app_name           # overrides the stored name in answers (tampering)
        # Status sequence after each start/restart, one per read (the last one repeats).
        self.app_statuses = app_statuses or ["starting:unhealthy", "running:healthy"]
        self.sensitive = sensitive         # token has read:sensitive
        self.scoped = scoped               # token limited to one Coolify project
        self.service_extra = {}            # e.g. {"service_type": "wordpress"} in GET answers
        self.app_log_text = "listening on :8080"
        self.fail = {}                     # (method, path) -> (status, body)

    def __call__(self, method, url, **kw):
        if "/internal/website" in url:     # Coolify deployments are authz-linked projects
            if self.authz is None:
                from .test_authz_integration import FakeAuthz
                self.authz = FakeAuthz()
            return self.authz(method, url, **kw)
        path = url.split("/api/v1", 1)[1].split("?")[0]
        self.calls.append({"method": method, "path": path, "json": kw.get("json"), "params": kw.get("params")})
        if (method, path) in self.fail:
            status, body = self.fail[(method, path)]
            return _Resp(body, status)
        return _Resp(*self._route(method, path, kw.get("json") or {}))

    # -- helpers
    @property
    def compose_raw(self):
        """The compose file Coolify stored for the last service touched."""
        return next(reversed(self.services.values()))["compose"] if self.services else ""

    def uuids(self):
        return list(self.services)

    def _status(self, svc):
        if svc["queue"]:
            svc["status"] = svc["queue"].pop(0) if len(svc["queue"]) > 1 else svc["queue"][0]
        return svc["status"]

    def _compose(self, raw):
        try:
            text = base64.b64decode(raw, validate=True).decode("utf-8")
            return text, json.loads(text)
        except (ValueError, TypeError):
            return None, None

    def _urls(self, svc, urls):
        for item in urls or []:
            if item.get("name") not in svc["containers"]:
                return f"Service container with '{item.get('name')}' not found."
            svc["fqdn"][item["name"]] = item.get("url")
        return ""

    def _check(self, body, allowed):
        extra = set(body) - allowed
        if extra:
            return {"message": "Validation failed.", "errors": {k: ["This field is not allowed."] for k in extra}}
        if "description" in body and body["description"] and not DESCRIPTION_PATTERN.match(body["description"]):
            return {"message": "Validation failed.", "errors": {"description": ["The description format is invalid."]}}
        if "name" in body and not NAME_PATTERN.match(body["name"] or ""):
            return {"message": "Validation failed.", "errors": {"name": ["The name format is invalid."]}}
        return None

    def _answer(self, uuid, svc):
        out = {"uuid": uuid, "name": self.app_name or svc["name"], "description": svc["description"],
               "service_type": None, "connect_to_docker_network": svc["connect"], "status": self._status(svc),
               "applications": [{"name": n, "fqdn": svc["fqdn"].get(n)} for n in svc["containers"]],
               "databases": []}
        if self.sensitive:
            out["docker_compose_raw"] = svc["compose"]
        return {**out, **self.service_extra}

    def _route(self, method, path, body):
        if path == "/version":
            return "4.7.5", 200
        if path in ("/servers", "/security/keys") and self.scoped:
            return {"message": "This API token is limited to selected projects, environments, or resources, "
                               "so it cannot use this endpoint."}, 403
        if path == "/servers":
            return [{"uuid": "srv-1", "name": "node-1", "ip": "10.0.0.1"}], 200
        if path == "/projects":
            return [{"uuid": "proj-1", "name": "students"}], 200
        if path == "/security/keys":
            return [{"uuid": "key-1", "name": "kova-deploy-key"}], 200
        if path == "/services" and method == "POST":
            bad = self._check(body, CREATE_FIELDS)
            if bad:
                return bad, 422
            text, doc = self._compose(body.get("docker_compose_raw"))
            if doc is None:
                return {"message": "Validation failed.",
                        "errors": {"docker_compose_raw": "The docker_compose_raw should be base64 encoded."}}, 422
            uuid = f"svc-{self.next_uuid}"
            self.next_uuid += 1
            svc = {"name": body.get("name"), "description": body.get("description"), "compose": text,
                   "containers": list(doc.get("services") or {}), "connect": bool(body.get("connect_to_docker_network")),
                   "fqdn": {}, "envs": {}, "status": "exited", "queue": []}
            problem = self._urls(svc, body.get("urls"))
            if problem:                    # Coolify deletes the new service again
                return {"message": "Validation failed.", "errors": [problem]}, 422
            self.services[uuid] = svc
            return {"uuid": uuid, "domains": sorted(u for u in svc["fqdn"].values() if u)}, 201
        m = re.match(r"^/services/([^/]+)(/.*)?$", path)
        if not m:
            return {"message": "Not found."}, 404
        uuid, sub = m.group(1), m.group(2) or ""
        svc = self.services.get(uuid)
        if svc is None:
            return {"message": "Service not found."}, 404
        if sub == "" and method == "GET":
            return self._answer(uuid, svc), 200
        if sub == "" and method == "PATCH":
            bad = self._check(body, UPDATE_FIELDS)
            if bad:
                return bad, 422
            if "docker_compose_raw" in body:
                text, doc = self._compose(body["docker_compose_raw"])
                if doc is None:
                    return {"message": "Validation failed."}, 422
                svc["compose"], svc["containers"] = text, list(doc.get("services") or {})
            if "description" in body:
                svc["description"] = body["description"]
            if "connect_to_docker_network" in body:
                svc["connect"] = bool(body["connect_to_docker_network"])
            problem = self._urls(svc, body.get("urls"))
            if problem:
                return {"message": "Validation failed.", "errors": [problem]}, 422
            return {"uuid": uuid, "domains": sorted(u for u in svc["fqdn"].values() if u)}, 200
        if sub == "" and method == "DELETE":
            del self.services[uuid]
            return {"message": "Service deletion request queued."}, 200
        if sub == "/envs/bulk" and method == "PATCH":
            for item in body.get("data") or []:
                item = {k: v for k, v in item.items() if k in ENV_FIELDS}
                svc["envs"][item["key"]] = item
            return [], 201
        if sub in ("/start", "/stop", "/restart") and method == "GET":
            return {"message": "This endpoint requires a POST request."}, 400
        if sub == "/start":
            if "running" in svc["status"]:
                return {"message": "Service is already running."}, 400
            svc["queue"] = list(self.app_statuses)
            return {"message": "Service starting request queued."}, 200
        if sub == "/restart":
            svc["queue"] = list(self.app_statuses)
            return {"message": "Service restaring request queued."}, 200
        if sub == "/stop":
            if "stopped" in svc["status"] or "exited" in svc["status"]:
                return {"message": "Service is already stopped."}, 400
            svc["status"], svc["queue"] = "exited", []
            return {"message": "Service stopping request queued."}, 200
        if sub == "/logs":
            if not self.sensitive:
                return {"message": "Missing required permissions: read:sensitive"}, 403
            return {"logs": self.app_log_text}, 200
        return {"message": "Not found."}, 404


class _Resp:
    def __init__(self, payload, status):
        self._p, self.status_code = payload, status
        self.text = json.dumps(payload)

    def json(self):
        return self._p


SAFE_DOCKERFILE = 'FROM alpine:3.20\nCMD ["true"]\n'


def created(fake):
    return next(c for c in fake.calls if (c["method"], c["path"]) == ("POST", "/services"))["json"]


def compose_sent(fake, call=None):
    return json.loads(base64.b64decode((call or created(fake))["docker_compose_raw"]))


def paths(fake):
    return [(c["method"], c["path"]) for c in fake.calls]


def coolify_effective_networks(doc: dict, service_uuid: str = "svc-x") -> dict:
    """Which networks each container joins after Coolify 4.7.5's serviceParser
    (bootstrap/helpers/parsers.php): every top-level network is added to every
    service without network_mode (top-level entries with ``aliases`` are skipped),
    then the service's own network, named after its uuid."""
    top = doc.get("networks") or {}
    out = {}
    for name, svc in (doc.get("services") or {}).items():
        if svc.get("network_mode") is not None:
            out[name] = {f"network_mode:{svc['network_mode']}"}
            continue
        nets = set(svc.get("networks") or {})
        nets |= {n for n, d in top.items() if n != "default" and not (d or {}).get("aliases")}
        out[name] = nets | {service_uuid}
    return out


class CoolifyCase(PlatformCase):
    """Coolify deployments exist only as linked, public projects built from a generated
    compose file (engine.coolify_refusal). This fixture provides authz, a Dockerfile
    project linked on creation, and the Dockerfile the build would read (no network
    access in tests)."""
    auto_link = True

    def setUp(self):
        super().setUp()
        from .test_authz_integration import WEBSITE_KEY
        self.secret_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.secret_dir, True)
        self.app.config.update(AUTHZ_URL="http://authz.test:8010", AUTHZ_WEBSITE_KEY=WEBSITE_KEY,
                               COOLIFY_READY_TIMEOUT=1)
        self.dockerfile = SAFE_DOCKERFILE
        patcher = mock.patch("kova.buildpolicy.fetch_dockerfile", side_effect=lambda *a, **k: self.dockerfile)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The readiness check's HTTP request to the app (no network in tests): answers
        # unless a test sets self.probe to something else.
        self.probe = lambda url, base="": (True, "HTTP 200")
        for patcher in (mock.patch("kova.engine.probe_app", side_effect=lambda *a, **k: self.probe(*a, **k)),
                        mock.patch("kova.engine.POLL_SECONDS", 0.01),
                        mock.patch("kova.engine.START_GRACE", 0.3)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def create(self, **extra):
        r = super().create(**{"framework": "docker", **extra})
        project = (r.get_json() or {}).get("project")
        if self.auto_link and project:
            conn = connect(self.app.config["DATABASE"])
            conn.execute("UPDATE projects SET authz_project_id = 'prj_0123456789abcdef' WHERE slug = ?",
                         (project["slug"],))
            conn.commit()
            conn.close()
        return r

    def configure_coolify(self, **extra):
        conn = connect(self.app.config["DATABASE"])
        for k, v in {"coolify_url": "https://coolify.example.com", "coolify_token": "secret-coolify-token",
                     "coolify_project_uuid": "proj-1", "coolify_server_uuid": "srv-1",
                     "coolify_base_domain": "apps.example.com", "deploy_provider": "coolify", **extra}.items():
            set_setting(conn, k, v)
        conn.close()

    def deploy_with(self, fake, **project):
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            return self.wait(self.create(**project).get_json()["deployment_id"])

    def logs(self, dep_id):
        return [r["line"] for r in self.app_conn().execute(
            "SELECT line FROM deploy_logs WHERE deployment_id = ? ORDER BY id", (dep_id,))]


class CoolifyDeploymentTests(CoolifyCase):
    def setUp(self):
        super().setUp()
        self.configure_coolify()
        self.as_student()

    def test_full_lifecycle_against_a_mock_server(self):
        fake = FakeCoolify()
        d = self.deploy_with(fake)
        self.assertEqual(d["status"], "ready", d)
        self.assertEqual(d["provider"], "coolify")
        self.assertFalse(d["is_simulated"], "a Coolify build must never be flagged simulated")
        self.assertEqual(d["url"], "https://web-app.apps.example.com")
        self.assertTrue(d["expires_at"])

        p = paths(fake)
        self.assertIn(("POST", "/services"), p)
        self.assertIn(("POST", "/services/svc-123/start"), p)
        # the service is verified (GET) after the last change and before the start
        self.assertLess(p.index(("GET", "/services/svc-123")), p.index(("POST", "/services/svc-123/start")))

        body = created(fake)
        self.assertEqual(body["project_uuid"], "proj-1")
        self.assertEqual(body["server_uuid"], "srv-1")
        self.assertEqual(body["environment_name"], "production")
        self.assertEqual(body["name"], "web-app")
        self.assertFalse(body["instant_deploy"], "Codeskys drives starts; Coolify must not also start it")
        self.assertIs(body["connect_to_docker_network"], False)
        project = self.app_conn().execute("SELECT * FROM projects WHERE slug = 'web-app'").fetchone()
        self.assertEqual(body["description"], engine.owner_marker(project))
        self.assertEqual(body["description"], f"Managed by Codeskys - project {project['id']} (web-app). Do not edit.")
        # Coolify routes Host(web-app.apps.example.com) to container port 3000
        self.assertEqual(body["urls"], [{"name": "app", "url": "https://web-app.apps.example.com:3000"}])
        app = compose_sent(fake)["services"]["app"]
        self.assertEqual(app["mem_limit"], "512M")
        self.assertEqual(app["cpus"], 0.5)
        self.assertEqual(app["expose"], ["3000"])

        text = " ".join(self.logs(d["id"]))
        self.assertIn("App service svc-123 created", text)
        self.assertIn("Health check passed", text)
        self.assertNotIn("coolify", text.lower(), "students never see the backend product name")
        row = self.app_conn().execute("SELECT provider_ref, coolify_app_name FROM projects").fetchone()
        self.assertEqual(tuple(row), ("svc-123", "web-app"))

    def test_the_ownership_marker_passes_coolifys_validation(self):
        project = {"id": 4095, "slug": "a-b-c9"}
        self.assertRegex(engine.owner_marker(project), DESCRIPTION_PATTERN)
        self.assertNotRegex("Managed by Codeskys: project 1 (x).", DESCRIPTION_PATTERN,
                            "':' is outside Coolify's description character set (422)")

    def test_redeploy_patches_the_service_and_restarts_it(self):
        fake = FakeCoolify()
        self.assertEqual(self.deploy_with(fake)["status"], "ready")
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            dep = self.call("post", "/api/projects/web-app/deploy").get_json()["deployment"]
            d = self.wait(dep["id"])
        self.assertEqual(d["status"], "ready", d)
        p = paths(fake)
        self.assertNotIn(("POST", "/services"), p, "one service per project, reused")
        patch = next(c for c in fake.calls if (c["method"], c["path"]) == ("PATCH", "/services/svc-123"))["json"]
        self.assertEqual(set(patch), {"description", "docker_compose_raw", "connect_to_docker_network", "urls"})
        self.assertIs(patch["connect_to_docker_network"], False)
        # Coolify refuses to start a running service: Codeskys restarts it instead.
        self.assertEqual([x for x in p if x[1].endswith(("/start", "/restart"))],
                         [("POST", "/services/svc-123/start"), ("POST", "/services/svc-123/restart")])
        # ownership is checked before the PATCH
        self.assertLess(p.index(("GET", "/services/svc-123")), p.index(("PATCH", "/services/svc-123")))

    def test_a_redeploy_coolify_never_starts_does_not_pass_off_the_old_version(self):
        fake = FakeCoolify()
        self.assertEqual(self.deploy_with(fake)["status"], "ready")
        fake.app_statuses = ["running:healthy"]          # the restart stays queued in Coolify
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            dep = self.call("post", "/api/projects/web-app/deploy").get_json()["deployment"]
            d = self.wait(dep["id"])
        self.assertEqual(d["status"], "failed")
        self.assertIn("did not start within", d["error"])
        self.assertFalse(d["expires_at"])

    def test_failed_remote_build_is_reported_as_failed(self):
        fake = FakeCoolify(app_statuses=["starting:unhealthy", "exited"])
        d = self.deploy_with(fake)
        self.assertEqual(d["status"], "failed")
        self.assertIn("did not become available", d["error"])
        self.assertIn("exited", d["error"])

    def test_api_error_surfaces_and_never_reports_success(self):
        authz = FakeCoolify()

        def broken(method, url, **kw):
            if "/internal/website" in url:
                return authz(method, url, **kw)
            return _Resp({"message": "Insufficient permissions"}, 403)

        d = self.deploy_with(broken)
        self.assertEqual(d["status"], "failed")
        self.assertIn("403", d["error"])

    def test_unreachable_coolify_fails_the_build_with_a_clear_message(self):
        import requests as rq
        authz = FakeCoolify()

        def unreachable(method, url, **kw):
            if "/internal/website" in url:
                return authz(method, url, **kw)
            raise rq.ConnectionError("boom")

        d = self.deploy_with(unreachable)
        self.assertEqual(d["status"], "failed")
        self.assertIn("Could not reach the deployment engine", d["error"])
        self.assertNotIn("coolify", d["error"].lower(), "nor its internal address")

    def test_private_repositories_are_refused(self):
        self._connect_github()
        fake = FakeCoolify()
        d = self.deploy_with(fake, is_private=True)
        self.assertEqual(d["status"], "failed")
        self.assertIn("Private repositories", d["error"])
        self.assertEqual(fake.calls, [], "no Coolify service may be created for a refused project")

    def test_no_public_address_creates_nothing(self):
        self.configure_coolify(coolify_base_domain="")
        fake = FakeCoolify()
        d = self.deploy_with(fake)
        self.assertEqual(d["status"], "failed")
        self.assertIn("no public address", d["error"])
        self.assertEqual(fake.calls, [])

    def test_a_step_before_the_start_fails_and_the_new_service_is_removed(self):
        fake = FakeCoolify()
        fake.fail[("PATCH", "/services/svc-123/envs/bulk")] = (422, {"message": "Validation failed."})
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            slug = self.create(deploy=False).get_json()["project"]["slug"]
            self.call("post", f"/api/projects/{slug}/env", {"key": "API_URL", "value": "https://x.test"})
            d = self.wait(self.call("post", f"/api/projects/{slug}/deploy").get_json()["deployment"]["id"])
        self.assertEqual(d["status"], "failed")
        self.assertIn("422", d["error"])
        self.assertIn(("DELETE", "/services/svc-123"), paths(fake))
        self.assertNotIn(("POST", "/services/svc-123/start"), paths(fake))
        self.assertEqual(fake.uuids(), [])
        self.assertIsNone(self.app_conn().execute("SELECT provider_ref FROM projects").fetchone()[0])

    def test_a_service_that_is_not_ours_is_never_changed(self):
        fake = FakeCoolify()
        self.assertEqual(self.deploy_with(fake)["status"], "ready")
        fake.app_name = "production-billing-api"
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            d = self.wait(self.call("post", "/api/projects/web-app/deploy").get_json()["deployment"]["id"])
        self.assertEqual(d["status"], "failed")
        self.assertIn("Refusing to touch app service svc-123", d["error"])
        self.assertEqual({m for m, _ in paths(fake)}, {"GET"}, "nothing is changed, started or deleted")

    def test_reserved_and_coolify_magic_variable_names_are_not_passed(self):
        fake = FakeCoolify()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            slug = self.create(deploy=False).get_json()["project"]["slug"]
            for key in ("API_URL", "SERVICE_FQDN_APP", "COOLIFY_URL", "PORT"):
                self.call("post", f"/api/projects/{slug}/env", {"key": key, "value": "v"})
            d = self.wait(self.call("post", f"/api/projects/{slug}/deploy").get_json()["deployment"]["id"])
        self.assertEqual(d["status"], "ready", d)
        env = compose_sent(fake)["services"]["app"]["environment"]
        self.assertEqual(env, {"API_URL": "${API_URL}", "PORT": "3000"})
        sent = fake.services["svc-123"]["envs"]
        self.assertEqual(set(sent), {"API_URL"})
        self.assertTrue(sent["API_URL"]["is_literal"], "Coolify must not expand ${...} in student values")
        self.assertTrue(any("SERVICE_FQDN_APP" in line and "reserved" in line for line in self.logs(d["id"])))

    def test_no_deployment_credential_is_requested_for_coolify(self):
        self.app.config["RUNTIME_SECRET_DIR"] = self.secret_dir       # even with the sidecar sink configured
        fake = FakeCoolify()
        d = self.deploy_with(fake)
        self.assertEqual(d["status"], "ready", d)
        self.assertFalse(any(c["path"] == "/deployments" for c in fake.authz.calls))
        self.assertFalse(d["authz_deployment_id"])
        self.assertTrue(any("Protected Codeskys services are not available" in line for line in self.logs(d["id"])))

    def _connect_github(self):
        from .test_providers import FakeResponse
        self.app.config["GITHUB_CLIENT_ID"] = "cid"
        self.app.config["GITHUB_CLIENT_SECRET"] = "csecret"
        url = self.client.get("/api/git/github/authorize").get_json()["url"]
        state = url.split("state=")[1].split("&")[0]
        with mock.patch("kova.providers.requests.post",
                        return_value=FakeResponse({"access_token": "gho_tok", "scope": "repo"})), \
             mock.patch("kova.providers.requests.get",
                        return_value=FakeResponse({"id": 7, "login": "student1", "name": "S"})):
            self.client.get(f"/api/git/github/callback?code=abc&state={state}")


class CancellationTests(CoolifyCase):
    def setUp(self):
        super().setUp()
        self.configure_coolify()
        self.as_student()

    def test_cancel_before_the_start_removes_the_new_service(self):
        fake = FakeCoolify()
        conn = self.app_conn()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            dep_id = self.create().get_json()["deployment_id"]
            # Ask for cancellation once the service exists, before it is started.
            real = fake._route

            def route(method, path, body):
                if (method, path) == ("GET", "/services/svc-123"):
                    engine.request_cancel(conn, dep_id)
                return real(method, path, body)
            fake._route = route
            self.work()
        d = self.client.get(f"/api/deployments/{dep_id}").get_json()["deployment"]
        self.assertEqual(d["status"], "cancelled")
        self.assertNotIn(("POST", "/services/svc-123/start"), paths(fake))
        self.assertIn(("DELETE", "/services/svc-123"), paths(fake))

    def test_cancel_during_the_build_stops_the_service_and_keeps_it_stopped(self):
        fake = FakeCoolify(app_statuses=["starting:unhealthy"])
        conn = self.app_conn()
        real = fake._route

        def route(method, path, body):
            out = real(method, path, body)
            # The student presses Cancel while Coolify is building.
            if (method, path) == ("GET", "/services/svc-123") and out[0].get("status", "").startswith("starting"):
                engine.request_cancel(conn, conn.execute("SELECT MAX(id) FROM deployments").fetchone()[0])
            return out
        fake._route = route
        d = self.deploy_with(fake)
        self.assertEqual(d["status"], "cancelled")
        self.assertIn(("POST", "/services/svc-123/stop"), paths(fake))
        self.assertFalse(d["expires_at"])
        job = conn.execute("SELECT * FROM jobs WHERE kind = 'coolify_quiesce'").fetchone()
        self.assertIsNotNone(job, "a build still running on the server is stopped again later")

        # Coolify's build finished after the stop and started the container anyway.
        fake.services["svc-123"]["status"], fake.services["svc-123"]["queue"] = "running:healthy", []
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            with self.assertRaises(RuntimeError):
                engine.quiesce_cancelled_build(conn, self.app.config, json.loads(job["payload"]), None)
        self.assertIn(("POST", "/services/svc-123/stop"), paths(fake))
        self.assertTrue(fake.services["svc-123"]["status"].startswith("exited"))

    def test_quiesce_leaves_a_newer_deployment_alone(self):
        fake = FakeCoolify()
        d = self.deploy_with(fake)
        conn = self.app_conn()
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            engine.quiesce_cancelled_build(conn, self.app.config, {"project_id": d["project_id"],
                                                                   "app_ref": "svc-123",
                                                                   "deployment_id": d["id"] - 1}, None)
        self.assertEqual(fake.calls, [])

    def test_quiesce_never_stops_a_foreign_service(self):
        fake = FakeCoolify()
        d = self.deploy_with(fake)
        fake.app_name = "someone-else"
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            engine.quiesce_cancelled_build(self.app_conn(), self.app.config,
                                           {"project_id": d["project_id"], "app_ref": "svc-123",
                                            "deployment_id": d["id"]}, None)
        self.assertNotIn("POST", [m for m, _ in paths(fake)])


class CoolifyTeardownTests(CoolifyCase):
    def _deploy(self, fake):
        self.configure_coolify()
        self.as_student()
        d = self.deploy_with(fake)
        self.assertEqual(d["status"], "ready", d)
        return d

    def _expire(self, fake, d):
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            engine.expire_deployment(self.app_conn(), self.app.config, {"deployment_id": d["id"]}, lambda *a, **k: True)

    def test_expiry_stops_and_deletes_only_the_codeskys_service(self):
        fake = FakeCoolify()
        d = self._deploy(fake)
        fake.calls.clear()
        self._expire(fake, d)
        self.assertIn(("POST", "/services/svc-123/stop"), paths(fake))
        self.assertIn(("DELETE", "/services/svc-123"), paths(fake))
        delete = next(c for c in fake.calls if c["method"] == "DELETE")
        self.assertEqual(delete["params"], {"delete_configurations": "true", "delete_volumes": "true",
                                            "docker_cleanup": "true", "delete_connected_networks": "true"})
        conn = self.app_conn()
        row = conn.execute("SELECT status, teardown_note FROM deployments WHERE id = ?", (d["id"],)).fetchone()
        self.assertEqual(row["status"], "expired")
        self.assertIn("stopped and deleted", row["teardown_note"])
        self.assertIsNone(conn.execute("SELECT provider_ref FROM projects WHERE slug='web-app'").fetchone()[0])

    def test_refuses_to_delete_a_service_it_did_not_create(self):
        for tamper in ({"app_name": "production-billing-api"}, {"description": "My own app"},
                       {"service_type": "wordpress"}):
            with self.subTest(tamper=tamper):
                self.setUp_fresh()
                self.as_student()
                self.upgrade_student()
                fake = FakeCoolify()
                d = self._deploy(fake)
                if "app_name" in tamper:
                    fake.app_name = tamper["app_name"]
                else:
                    fake.service_extra = tamper
                fake.calls.clear()
                self._expire(fake, d)
                self.assertEqual({m for m, _ in paths(fake)}, {"GET"})
                note = self.app_conn().execute("SELECT teardown_note FROM deployments WHERE id = ?",
                                               (d["id"],)).fetchone()[0]
                self.assertIn("Refused to delete", note)

    def setUp_fresh(self):
        conn = self.app_conn()
        conn.execute("DELETE FROM deployments")
        conn.execute("DELETE FROM projects")
        conn.commit()

    def test_non_live_deployment_expires_without_touching_coolify(self):
        fake = FakeCoolify()
        first = self._deploy(fake)
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            self.call("post", "/api/projects/web-app/deploy")
            self.work()
        fake.calls.clear()
        self._expire(fake, first)
        self.assertEqual(fake.calls, [], "superseded deployments must not tear down the live app")
        status = self.app_conn().execute("SELECT status FROM deployments WHERE id = ?", (first["id"],)).fetchone()[0]
        self.assertEqual(status, "expired")

    def test_already_deleted_service_is_treated_as_gone(self):
        fake = FakeCoolify()
        d = self._deploy(fake)
        fake.services.clear()                          # removed in the Coolify dashboard
        self._expire(fake, d)
        self.assertIsNone(self.app_conn().execute("SELECT provider_ref FROM projects").fetchone()[0])

    def test_project_deletion_deletes_only_its_own_service(self):
        fake = FakeCoolify()
        self._deploy(fake)
        fake.app_name = "someone-elses-service"
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.client.delete("/api/projects/web-app", json={"confirm": "web-app"},
                                   headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("DELETE", [m for m, _ in paths(fake)])
        self.assertIn("svc-123", fake.uuids())

        fake2 = FakeCoolify()
        self.as_student()
        d = self.deploy_with(fake2)
        self.assertEqual(d["status"], "ready", d)
        with mock.patch("kova.engine.requests.request", side_effect=fake2):
            r = self.client.delete("/api/projects/web-app", json={"confirm": "web-app"},
                                   headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.status_code, 200)
        self.assertIn(("DELETE", "/services/svc-123"), paths(fake2))
        self.assertEqual(fake2.uuids(), [])


class CoolifyAdminTests(CoolifyCase):
    def test_connection_test_lists_servers_projects_and_keys(self):
        self.configure_coolify()
        self.login(ADMIN[0], ADMIN[2])
        with mock.patch("kova.engine.requests.request", side_effect=FakeCoolify()):
            r = self.call("post", "/api/admin/integrations/coolify/test")
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], "4.7.5")
        self.assertEqual(data["servers"][0]["uuid"], "srv-1")
        self.assertEqual(data["private_keys"][0]["uuid"], "key-1")

    def test_connection_test_works_with_a_project_scoped_token(self):
        self.configure_coolify()
        self.login(ADMIN[0], ADMIN[2])
        with mock.patch("kova.engine.requests.request", side_effect=FakeCoolify(scoped=True)):
            r = self.call("post", "/api/admin/integrations/coolify/test")
        data = r.get_json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["servers"], [])
        self.assertEqual(data["projects"][0]["uuid"], "proj-1")

    def test_connection_failure_is_reported_honestly(self):
        self.configure_coolify()
        self.login(ADMIN[0], ADMIN[2])
        import requests as rq
        with mock.patch("kova.engine.requests.request", side_effect=rq.ConnectionError("x")):
            r = self.call("post", "/api/admin/integrations/coolify/test")
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()["ok"])

    def test_token_is_never_returned_to_the_browser(self):
        self.configure_coolify()
        self.login(ADMIN[0], ADMIN[2])
        body = self.client.get("/api/admin/integrations").get_data(as_text=True)
        self.assertNotIn("secret-coolify-token", body)
        self.assertIn("token_set", body)

    def test_half_configured_coolify_falls_back_to_the_simulator(self):
        self.configure_coolify(coolify_server_uuid="")
        self.as_student()
        d = self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(d["provider"], "simulator")
        self.assertTrue(d["is_simulated"])


class BuildIsolationTests(CoolifyCase):
    """Coolify's own build packs run `docker build --network host` (v4.7.5). These
    prove no deployment can reach them, that the service Coolify gets has nothing it
    could merge into a shared network, and that nothing is started unless the service
    is exactly what Codeskys created (kova/buildpolicy.py)."""

    LEGACY = ("/applications/public", "/applications/private-deploy-key", "/applications/private-github-app",
              "/applications/dockerfile", "/applications/dockerimage", "/applications/dockercompose", "/deploy")

    def run_one(self, fake=None, **project):
        self.configure_coolify()
        self.as_student()
        fake = fake or FakeCoolify()
        return self.deploy_with(fake, **project), fake

    def assert_nothing_started(self, d, fake):
        self.assertEqual(d["status"], "failed", d)
        self.assertFalse([p for _, p in paths(fake) if p.endswith(("/start", "/restart"))])
        self.assertFalse(any(c["path"] == "/deployments" for c in (fake.authz.calls if fake.authz else [])))

    def test_only_the_generated_compose_service_is_ever_used(self):
        d, fake = self.run_one()
        self.assertEqual(d["status"], "ready", d)
        self.assertFalse([p for _, p in paths(fake) if p in self.LEGACY or p.startswith("/applications")])
        body = created(fake)
        self.assertNotIn("type", body, "never a Coolify template")
        doc = compose_sent(fake)
        self.assertEqual(set(doc), {"services"})
        self.assertEqual(set(doc["services"]), {"app"}, "the student's app runs alone")
        app = doc["services"]["app"]
        self.assertEqual(set(app["build"]), {"context", "dockerfile"}, "no build network, entitlements or privileges")
        for key in ("networks", "network_mode", "volumes", "ports", "privileged", "cap_add", "devices", "pid"):
            self.assertNotIn(key, app)
        self.assertEqual(app["cap_drop"], ["ALL"])

    def test_after_coolifys_network_merge_the_app_is_only_on_its_own_network(self):
        _, fake = self.run_one()
        self.assertEqual(coolify_effective_networks(compose_sent(fake), "svc-123"), {"app": {"svc-123"}})

    def test_why_the_sidecar_compose_is_not_used_on_coolify(self):
        # The app+sidecar layout keeps the app off the runtime link network only if each
        # container keeps its own network list. Coolify 4.7.5 merges them:
        doc = composegen.generate(
            slug="web-app", deployment_id="dep_abcdef0123456789", repository_id=1,
            app_context=composegen.build_context("https://github.com/acme/web-app", "a" * 40),
            runtime_image="codeskys/runtime:local", secret_file="/x/dep_abcdef0123456789/runtime_secret",
            ingress_network="codeskys-apps", link_network="codeskys-runtime-link",
            private_cidr="10.213.0.16/28", project_id=1)
        merged = coolify_effective_networks(doc)
        self.assertNotIn("link", doc["services"]["app"]["networks"])
        self.assertIn("link", merged["app"], "Coolify would put the student app on the runtime link network")
        self.assertIn("ingress", merged["codeskys-runtime"])

    def test_every_framework_builds_from_a_codeskys_dockerfile_not_coolify_build_packs(self):
        from kova import dockerfiles
        from kova.gitinfo import FRAMEWORKS
        fetched = []
        with mock.patch("kova.buildpolicy.fetch_dockerfile", side_effect=lambda *a, **k: fetched.append(a) or SAFE_DOCKERFILE):
            for framework in [f for f in FRAMEWORKS if f != "docker"]:
                with self.subTest(framework=framework):
                    self.upgrade_student()
                    d, fake = self.run_one(framework=framework, name=f"App {framework}")
                    self.assertEqual(d["status"], "ready", d)
                    self.assertFalse([p for _, p in paths(fake) if p.startswith("/applications")])
                    doc = compose_sent(fake)
                    build = doc["services"]["app"]["build"]
                    self.assertEqual(set(build), {"context", "dockerfile_inline"})
                    self.assertTrue(build["dockerfile_inline"].startswith(dockerfiles.MARKER))
                    self.assertEqual(composegen._inline_problem(build["dockerfile_inline"]), "")
                    expected_port = 8080 if FRAMEWORKS[framework]["static"] else FRAMEWORKS[framework]["port"]
                    self.assertEqual(doc["services"]["app"]["expose"], [str(expected_port)])
                    self.assertTrue(created(fake)["urls"][0]["url"].endswith(f":{expected_port}"))
        self.assertEqual(fetched, [], "a generated Dockerfile needs no Dockerfile from the repository")

    def test_unsafe_build_settings_are_refused_before_coolify_is_asked(self):
        cases = (("build_command", "npm run build --network=host", "--network"),
                 ("install_command", "npm install" + chr(10) + "RUN --network=host x", "single line"),
                 ("start_command", "node --security=insecure x", "--network, --security"),
                 ("output_dir", "../../etc", "relative path"))
        self.configure_coolify()
        self.as_student()
        self.upgrade_student()
        for field, value, needle in cases:
            with self.subTest(field=field):
                slug = self.create(framework="react", name=f"App {field}", deploy=False).get_json()["project"]["slug"]
                conn = self.app_conn()
                conn.execute(f"UPDATE projects SET {field} = ? WHERE slug = ?", (value, slug))
                conn.commit()
                fake = FakeCoolify()
                with mock.patch("kova.engine.requests.request", side_effect=fake):
                    d = self.wait(self.call("post", f"/api/projects/{slug}/deploy").get_json()["deployment"]["id"])
                self.assert_nothing_started(d, fake)
                self.assertIn(needle, d["error"])
                self.assertEqual(fake.calls, [])

    def test_dockerfile_asking_for_host_access_is_refused_before_coolify_is_asked(self):
        cases = {
            "RUN --network=host curl http://127.0.0.1:8000": "--network",
            "RUN --security=insecure id": "--security",
            "RUN --device=/dev/kvm true": "--device",
            "RUN --net\\\nwork=host true": "--network",                   # split by a line continuation
            "ONBUILD RUN --NETWORK=HOST true": "--NETWORK",
        }
        for line, flag in cases.items():
            with self.subTest(line=line):
                self.upgrade_student()
                self.dockerfile = f"FROM alpine:3.20\n{line}\n"
                d, fake = self.run_one(name=f"App {len(line)}")
                self.assert_nothing_started(d, fake)
                self.assertIn(flag, d["error"])
                self.assertEqual(fake.calls, [])
        self.upgrade_student()
        self.dockerfile = "# syntax=evil.example/frontend:1\nFROM alpine:3.20\n"
        d, fake = self.run_one(name="App syntax")
        self.assert_nothing_started(d, fake)
        self.assertIn("syntax", d["error"])

    def test_an_unsafe_service_state_blocks_the_start_and_is_rolled_back(self):
        for extra, needle in (({"service_type": "wordpress"}, "template service"),
                              ({"connect_to_docker_network": True}, "destination's Docker network"),
                              ({"applications": [{"name": "app"}, {"name": "miner"}]}, "not one 'app'")):
            with self.subTest(extra=extra):
                self.upgrade_student()
                fake = FakeCoolify()
                fake.service_extra = extra
                d, fake = self.run_one(fake, name=f"App {needle[:10]}")
                self.assert_nothing_started(d, fake)
                self.assertIn(needle, d["error"])
                if "service_type" not in extra:            # a template is not ours: never deleted
                    self.assertEqual(fake.uuids(), [], "the service this deploy created is removed")

    def test_a_tampered_compose_file_blocks_the_start(self):
        unsafe = composegen.generate_service(slug="web-app", deployment=1, port=3000,
                                             app_context=composegen.build_context("https://github.com/acme/web-app",
                                                                                  "a" * 40))
        unsafe["services"]["app"]["network_mode"] = "host"
        other = composegen.generate_service(slug="web-app", deployment=99, port=3000,
                                            app_context=composegen.build_context("https://github.com/acme/web-app",
                                                                                 "a" * 40))
        for stored, message in ((unsafe, "not a safe Codeskys compose file"),
                                (other, "not the one Codeskys generated")):
            with self.subTest(message=message):
                self.upgrade_student()
                fake = FakeCoolify(sensitive=True)         # a token that can read the compose file
                fake.service_extra = {"docker_compose_raw": json.dumps(stored)}
                d, fake = self.run_one(fake, name=f"App {len(message)}")
                self.assert_nothing_started(d, fake)
                self.assertIn(message, d["error"])

    def test_start_and_restart_refuse_an_unsafe_service_but_stop_still_works(self):
        d, fake = self.run_one()
        self.assertEqual(d["status"], "ready", d)
        fake.service_extra = {"connect_to_docker_network": True}
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            for action in ("start", "restart"):
                r = self.call("post", f"/api/projects/web-app/app/{action}")
                self.assertEqual(r.status_code, 502, r.get_json())
                self.assertIn("destination's Docker network", r.get_json()["error"])
            self.assertEqual(self.call("post", "/api/projects/web-app/app/stop").status_code, 200)
        self.assertFalse([p for _, p in paths(fake) if p.endswith(("/start", "/restart"))])

    def test_stop_refuses_a_service_codeskys_did_not_create(self):
        d, fake = self.run_one()
        fake.app_name = "owner-dashboard-app"
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.call("post", "/api/projects/web-app/app/stop")
        self.assertEqual(r.status_code, 502)
        self.assertIn("Refusing to touch", r.get_json()["error"])
        self.assertEqual({m for m, _ in paths(fake)}, {"GET"})


class AppControlTests(CoolifyCase):
    def test_stop_start_restart_and_status_reach_the_service(self):
        self.configure_coolify()
        self.as_student()
        fake = FakeCoolify()
        self.assertEqual(self.deploy_with(fake)["status"], "ready")
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            for action in ("stop", "start", "restart"):
                r = self.call("post", f"/api/projects/web-app/app/{action}")
                self.assertEqual(r.status_code, 200, (action, r.get_json()))
            status = self.client.get("/api/projects/web-app/app")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["fqdn"], "https://web-app.apps.example.com",
                         "the container port Codeskys gave Coolify is not shown")
        self.assertEqual([p for p in paths(fake) if p[0] == "POST"],
                         [("POST", "/services/svc-123/stop"), ("POST", "/services/svc-123/start"),
                          ("POST", "/services/svc-123/restart")])

    def test_sweep_does_not_touch_live_services(self):
        self.configure_coolify()
        self.as_student()
        fake = FakeCoolify()
        self.deploy_with(fake)
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            jobs.sweep(self.app_conn(), self.app.config)
        self.assertEqual(fake.calls, [])
