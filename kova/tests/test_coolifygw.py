"""The Coolify API gateway forwards only what the Codeskys adapter sends."""
import base64
import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kova import composegen, coolifygw
from kova.coolifygw import Refused, check

SHA = "a" * 40
CFG = {"upstream": "", "token_file": "", "project_uuid": "proj1234abcd", "server_uuid": "srv1234abcd",
       "destination_uuid": "dst1234abcd", "environment_name": "production", "base_domain": "apps.codeskys.com"}
UUID = "svc1234abcd5678"


def compose(**over):
    args = dict(slug="web-app", deployment=7, port=3000, env_keys=["API_URL"],
                app_context=composegen.build_context("https://github.com/octocat/Hello-World", SHA))
    args.update(over)
    return composegen.generate_service(**args)


def b64(doc):
    return base64.b64encode(composegen.render(doc).encode()).decode()


def create_body(**over):
    body = {"project_uuid": CFG["project_uuid"], "server_uuid": CFG["server_uuid"],
            "destination_uuid": CFG["destination_uuid"], "environment_name": "production", "name": "web-app",
            "instant_deploy": False, "connect_to_docker_network": False,
            "description": "Managed by Codeskys - project 3 (web-app). Do not edit.",
            "docker_compose_raw": b64(compose()),
            "urls": [{"name": "app", "url": "http://web-app.apps.codeskys.com:3000"}]}
    body.update(over)
    return body


class CheckTests(unittest.TestCase):
    def refused(self, method, path, body=None, query=None):
        with self.assertRaises(Refused):
            check(method, path, query or {}, body, CFG)

    def test_the_adapter_calls_pass(self):
        check("POST", "/services", {}, create_body(), CFG)
        patch = {k: create_body()[k] for k in ("description", "docker_compose_raw", "connect_to_docker_network", "urls")}
        check("PATCH", f"/services/{UUID}", {}, patch, CFG)
        check("PATCH", f"/services/{UUID}/envs/bulk", {}, {"data": [{"key": "API_URL", "value": "x", "is_literal": True}]}, CFG)
        for method, path, q in (("GET", "/version", {}), ("GET", "/services", {}), ("GET", f"/services/{UUID}", {}),
                                ("POST", f"/services/{UUID}/start", {}), ("POST", f"/services/{UUID}/restart", {}),
                                ("POST", f"/services/{UUID}/stop", {"docker_cleanup": "true"}),
                                ("GET", f"/services/{UUID}/logs", {"sub_service_name": "app", "lines": "40"}),
                                ("DELETE", f"/services/{UUID}", {"delete_configurations": "true", "delete_volumes": "true",
                                                                 "docker_cleanup": "true", "delete_connected_networks": "true"}),
                                ("GET", "/projects", {}), ("GET", "/servers", {}), ("GET", "/security/keys", {})):
            with self.subTest(method=method, path=path):
                check(method, path, q, None, CFG)

    def test_other_endpoints_are_refused(self):
        for method, path in (("PATCH", "/servers/abcdefgh1"), ("PUT", "/servers/abcdefgh1/proxy/configuration"),
                             ("POST", "/servers/abcdefgh1/proxy/restart"), ("POST", "/security/keys"),
                             ("PATCH", "/team"), ("POST", "/applications/dockerfile"), ("GET", "/applications"),
                             ("POST", "/projects"), ("DELETE", "/projects/abcdefgh1"), ("GET", "/services/../servers"),
                             ("POST", f"/services/{UUID}/envs"), ("GET", f"/services/{UUID}/envs")):
            with self.subTest(method=method, path=path):
                self.refused(method, path)
        self.refused("GET", "/services", query={"team": "0"})
        self.refused("POST", f"/services/{UUID}/start", body={"x": 1})

    def test_a_service_outside_the_codeskys_project_is_refused(self):
        for key, value in (("project_uuid", "other"), ("server_uuid", "localhostuuid"),
                           ("destination_uuid", "coolifynet"), ("environment_name", "staging")):
            with self.subTest(key=key):
                self.refused("POST", "/services", create_body(**{key: value}))
        body = create_body()
        del body["destination_uuid"]
        self.refused("POST", "/services", body)

    def test_unsafe_service_options_are_refused(self):
        self.refused("POST", "/services", create_body(connect_to_docker_network=True))
        self.refused("POST", "/services", create_body(instant_deploy=True))
        self.refused("POST", "/services", create_body(service_type="postgresql"))
        self.refused("PATCH", f"/services/{UUID}", {"connect_to_docker_network": True})
        self.refused("PATCH", f"/services/{UUID}", {"name": "other"})

    def test_privileged_or_host_compose_files_are_refused(self):
        mutations = {
            "privileged": lambda a: a.update(privileged=True),
            "host network": lambda a: a.update(network_mode="host"),
            "host pid": lambda a: a.update(pid="host"),
            "host mount": lambda a: a.update(volumes=["/:/host"]),
            "docker socket": lambda a: a.update(volumes=["/var/run/docker.sock:/var/run/docker.sock"]),
            "cap_add": lambda a: a.update(cap_add=["SYS_ADMIN"]),
            "devices": lambda a: a.update(devices=["/dev/kvm"]),
            "ports": lambda a: a.update(ports=["80:3000"]),
            "networks": lambda a: a.update(networks=["coolify"]),
            "capabilities kept": lambda a: a.update(cap_drop=[]),
            "new privileges": lambda a: a.update(security_opt=["seccomp=unconfined"]),
            "build network": lambda a: a["build"].update(network="host"),
            "build entitlements": lambda a: a["build"].update(entitlements=["security.insecure"]),
            "image instead of build": lambda a: (a.pop("build"), a.update(image="alpine")),
            "local build context": lambda a: a["build"].update(context="/"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                doc = copy.deepcopy(compose())
                mutate(doc["services"]["app"])
                self.refused("POST", "/services", create_body(docker_compose_raw=b64(doc)))
        extra = copy.deepcopy(compose())
        extra["services"]["db"] = {"image": "postgres"}
        self.refused("POST", "/services", create_body(docker_compose_raw=b64(extra)))
        self.refused("POST", "/services", create_body(docker_compose_raw="bm90IGpzb24="))
        self.refused("POST", "/services", create_body(docker_compose_raw="%%%"))

    def test_urls_must_be_the_projects_own_app_hostname(self):
        for url in ("http://other.apps.codeskys.com:3000", "http://web-app.apps.codeskys.com:8080",
                    "https://web-app.apps.codeskys.com:3000", "http://codeskys.com:3000",
                    "http://web-app.apps.codeskys.com.evil.test:3000", "http://coolify:8080",
                    "http://web-app.apps.codeskys.com:3000,http://codeskys.com"):
            with self.subTest(url=url):
                self.refused("POST", "/services", create_body(urls=[{"name": "app", "url": url}]))
        self.refused("POST", "/services", create_body(urls=[{"name": "app", "url": "http://web-app.apps.codeskys.com:3000"},
                                                           {"name": "app", "url": "http://x.apps.codeskys.com:3000"}]))
        self.refused("PATCH", f"/services/{UUID}", {"urls": [{"name": "app", "url": "http://web-app.apps.codeskys.com:3000"}]})

    def test_the_description_must_be_the_ownership_marker_of_the_same_slug(self):
        self.refused("POST", "/services", create_body(description="anything"))
        self.refused("POST", "/services", create_body(description="Managed by Codeskys - project 3 (other). Do not edit."))
        self.refused("PATCH", f"/services/{UUID}", {"description": "Managed by Codeskys - project 4 (other). Do not edit.",
                                                    "docker_compose_raw": b64(compose()),
                                                    "urls": [{"name": "app", "url": "http://web-app.apps.codeskys.com:3000"}]})

    def test_environment_variables_are_plain_and_literal(self):
        for item in ({"key": "SERVICE_FQDN_APP", "value": "x"}, {"key": "COOLIFY_URL", "value": "x"},
                     {"key": "CODESKYS_RUNTIME_URL", "value": "x"}, {"key": "BAD-NAME", "value": "x"},
                     {"key": "OK", "value": 1}, {"key": "OK", "value": "x", "is_literal": False},
                     {"key": "OK", "value": "x", "is_build_time": True}):
            with self.subTest(item=item):
                self.refused("PATCH", f"/services/{UUID}/envs/bulk", {"data": [item]})


class _Upstream(BaseHTTPRequestHandler):
    seen = []

    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        type(self).seen.append((self.command, self.path, self.headers.get("Authorization"), self.rfile.read(n)))
        out = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PATCH = do_DELETE = _any

    def log_message(self, *a):
        pass


class GatewayHttpTests(unittest.TestCase):
    def setUp(self):
        _Upstream.seen = []
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        fd, self.token_file = tempfile.mkstemp()
        os.write(fd, b"1|realtoken-from-file\n")
        os.close(fd)
        coolifygw.Handler.cfg = dict(CFG, upstream=f"http://127.0.0.1:{self.up.server_address[1]}",
                                     token_file=self.token_file)
        coolifygw.Handler.log_message = lambda *a: None
        self.gw = ThreadingHTTPServer(("127.0.0.1", 0), coolifygw.Handler)
        threading.Thread(target=self.gw.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.gw.server_address[1]}/api/v1"

    def tearDown(self):
        self.up.shutdown(); self.gw.shutdown()
        os.unlink(self.token_file)

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer caller-token", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_allowed_calls_are_forwarded_with_the_gateways_token(self):
        status, out = self.call("POST", "/services", create_body())
        self.assertEqual(status, 201)
        method, path, auth, body = _Upstream.seen[-1]
        self.assertEqual((method, path), ("POST", "/api/v1/services"))
        self.assertEqual(auth, "Bearer 1|realtoken-from-file")
        self.assertEqual(json.loads(body)["name"], "web-app")
        self.assertNotIn("realtoken", json.dumps(out))

    def test_refused_calls_never_reach_coolify(self):
        status, out = self.call("PATCH", "/servers/abcdefgh1", {"description": "x"})
        self.assertEqual(status, 403)
        self.assertIn("Refused by the Codeskys gateway", out["message"])
        bad = copy.deepcopy(compose()); bad["services"]["app"]["privileged"] = True
        self.assertEqual(self.call("POST", "/services", create_body(docker_compose_raw=b64(bad)))[0], 403)
        self.assertIn(self.call("GET", "/../../api/v1/servers")[0], (403, 404))
        self.assertEqual(_Upstream.seen, [])

    def test_the_adapter_works_through_the_gateway(self):
        from kova.engine import Coolify
        api = Coolify(self.base[: -len("/api/v1")], "not-the-real-token")
        self.assertEqual(api.version()["path"], "/api/v1/version")
        api.stop_app(UUID)
        self.assertEqual(_Upstream.seen[-1][1], f"/api/v1/services/{UUID}/stop?docker_cleanup=true")
        self.assertTrue(all(s[2] == "Bearer 1|realtoken-from-file" for s in _Upstream.seen))
