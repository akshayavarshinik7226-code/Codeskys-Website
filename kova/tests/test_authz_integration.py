"""Website <-> Codeskys authorization (authz) integration.

Driven against fakes of the authz website API (forgeops/authz/WEBSITE_INTEGRATION.md)
and of Coolify. They prove the request contract, the error handling and the secret
handling rules. They are NOT evidence against the live authz or a live Coolify.
"""
import json
import os
import shutil
import tempfile
from unittest import mock

from kova import authz_client, runtime_secrets
from kova.authz_client import AuthzClient, AuthzError

from .base import ADMIN
from .test_coolify import CoolifyCase, FakeCoolify, _Resp
from .test_platform import REPO

WEBSITE_KEY = "website-key-should-never-leak-0123456789"
RUNTIME_SECRET = "csr_THIS-IS-THE-RUNTIME-SECRET-abcdef0123456789"
STUDENT_CREDENTIAL = "dpl_live_STUDENT-CREDENTIAL-0123456789abcdef"


class FakeAuthz:
    """Answers like authz's /internal/website/* API (v2 error envelope)."""

    def __init__(self):
        self.calls = []
        self.can_deploy = True
        self.deploy_status = 200
        self.down = False
        self.registered_repo = REPO
        self.registered_repo_id = None     # the project object's repository.github_repo_id
        self.revoked = []

    def deny(self, status, code, message="", ref="REQ-TEST00000001"):
        body = {"ok": False, "error": {"code": code, "reference": ref, "message": message}, "detail": message}
        return body, status

    def project(self, pid="prj_0123456789abcdef"):
        return {"project_id": pid, "project_name": "Web App", "repo_url": self.registered_repo,
                "can_deploy": self.can_deploy, "state": "active" if self.can_deploy else "project_suspended",
                "code": "" if self.can_deploy else authz_client.POLICY, "suspended": not self.can_deploy,
                "repository": None if self.registered_repo_id is None else
                {"github_repo_id": self.registered_repo_id, "full_name": "acme/web-app", "private": False,
                 "verified_by": "github_api", "github_app_installed": False},
                "deployments": []}

    def __call__(self, method, url, headers=None, json=None, **kw):
        path = url.split("/internal/website", 1)[1]
        self.calls.append({"method": method, "path": path, "json": json, "headers": headers or {}})
        if self.down:
            import requests
            raise requests.ConnectionError("down")
        if (headers or {}).get("X-Website-Key") != WEBSITE_KEY:
            return _Resp(*self.deny(401, authz_client.AUTH, "website authentication required"))
        if path == "/projects/link":
            if json.get("credential") != STUDENT_CREDENTIAL:
                return _Resp(*self.deny(403, authz_client.CRED, "EXECUTION CONTEXT INITIALIZATION FAILED\n\nx"))
            return _Resp(self.project(), 200)
        if path == "/projects" and method == "POST":
            return _Resp(self.project("prj_fedcba9876543210"), 200)
        if path.startswith("/projects/") and path.endswith("/repository"):
            return _Resp({"project_id": path.split("/")[2], "github_repo_id": 424242, "full_name": "acme/web-app",
                          "private": False, "verified_by": "github_api", "github_app_installed": False}, 200)
        if path.startswith("/projects/") and path.endswith("/suspend"):
            self.can_deploy = not json.get("suspended")
            return _Resp(self.project(path.split("/")[2]), 200)
        if path.startswith("/projects/"):
            if path.split("/")[2] == "prj_00000000deadbeef":
                return _Resp(*self.deny(404, authz_client.NOT_FOUND, "The requested resource was not found."))
            return _Resp(self.project(path.split("/")[2]), 200)
        if path == "/deployments":
            if self.deploy_status != 200:
                return _Resp(*self.deny(self.deploy_status, authz_client.REPO, ""))
            return _Resp({"deployment_id": "dep_abcdef0123456789", "project_id": json["project_id"],
                          "status": "active", "github_repo_id": json["github_repo_id"], "commit_sha": "",
                          "created_at": "2026-11-06T00:00:00+00:00", "expires_at": "2026-11-07T00:00:00+00:00",
                          "revoke_reason": "", "runtime_secret": RUNTIME_SECRET}, 200)
        if path.endswith("/revoke"):
            self.revoked.append(path.split("/")[2])
            return _Resp({"deployment_id": path.split("/")[2], "revoked": True}, 200)
        if path.startswith("/github/installations/"):
            return _Resp({"repository_selection": "selected", "total_count": 1,
                          "repositories": [{"id": 424242, "full_name": "acme/web-app", "private": True,
                                            "default_branch": "main", "extra": "dropped"}]}, 200)
        return _Resp({"ok": False, "error": {"code": "ERR_NOT_FOUND", "reference": "", "message": "nf"}}, 404)


class Router:
    """One requests.request replacement for both fakes (same requests module)."""

    def __init__(self, authz, coolify):
        self.authz, self.coolify = authz, coolify

    def __call__(self, method, url, **kw):
        if "/internal/website" in url:
            return self.authz(method, url, **kw)
        return self.coolify(method, url, **kw)


class ClientContractTests(CoolifyCase):
    def authz(self, fake):
        return AuthzClient("http://authz.test:8010", WEBSITE_KEY, session=mock.Mock(request=fake))

    def test_sends_website_key_and_v2_errors_header(self):
        fake = FakeAuthz()
        self.authz(fake).get_project("prj_0123456789abcdef")
        h = fake.calls[0]["headers"]
        self.assertEqual(h["X-Website-Key"], WEBSITE_KEY)
        self.assertEqual(h["X-Authz-Errors"], "v2")
        self.assertEqual(fake.calls[0]["path"], "/projects/prj_0123456789abcdef")

    def test_v2_envelope_is_parsed(self):
        fake = FakeAuthz()
        fake.deploy_status = 403
        with self.assertRaises(AuthzError) as cm:
            self.authz(fake).create_deployment("prj_0123456789abcdef", 1)
        e = cm.exception
        self.assertEqual((e.status, e.code, e.reference), (403, authz_client.REPO, "REQ-TEST00000001"))
        self.assertIn("registered repository", e.user_message())
        self.assertIn("REQ-TEST00000001", e.user_message())

    def test_opaque_banner_messages_are_replaced_with_plain_text(self):
        e = AuthzError(403, authz_client.CRED, "REQ-1", "EXECUTION CONTEXT INITIALIZATION FAILED\n\nThe ...")
        self.assertNotIn("EXECUTION CONTEXT", e.user_message())
        self.assertIn("not valid", e.user_message())

    def test_legacy_200_denial_and_detail_shapes(self):
        legacy = mock.Mock(return_value=_Resp({"ok": False, "reason": "project_suspended",
                                               "reference": "REQ-OLD"}, 200))
        with self.assertRaises(AuthzError) as cm:
            AuthzClient("http://a", "k", session=mock.Mock(request=legacy)).get_project("prj_0123456789abcdef")
        self.assertEqual((cm.exception.status, cm.exception.code), (200, authz_client.POLICY))
        old = mock.Mock(return_value=_Resp({"detail": "deployment not found"}, 404))
        with self.assertRaises(AuthzError) as cm:
            AuthzClient("http://a", "k", session=mock.Mock(request=old)).get_deployment("dep_abcdef0123")
        self.assertEqual(cm.exception.status, 404)

    def test_unreachable_is_retryable_and_401_is_an_operator_problem(self):
        fake = FakeAuthz()
        fake.down = True
        with self.assertRaises(AuthzError) as cm:
            self.authz(fake).get_project("prj_0123456789abcdef")
        self.assertEqual(cm.exception.status, 0)
        self.assertTrue(cm.exception.retryable)
        bad = AuthzClient("http://authz.test", "wrong-key", session=mock.Mock(request=FakeAuthz()))
        with self.assertRaises(AuthzError) as cm:
            bad.get_project("prj_0123456789abcdef")
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("misconfigured", cm.exception.user_message())

    def test_key_never_appears_in_repr_or_errors(self):
        fake = FakeAuthz()
        fake.deploy_status = 403
        c = self.authz(fake)
        self.assertNotIn(WEBSITE_KEY, repr(c))
        try:
            c.create_deployment("prj_0123456789abcdef", 1)
        except AuthzError as e:
            self.assertNotIn(WEBSITE_KEY, str(e) + e.user_message() + e.describe())

    def test_path_segments_are_validated(self):
        c = self.authz(FakeAuthz())
        for bad in ("../admin", "prj_1/../../x", "a b", ""):
            with self.assertRaises(AuthzError):
                c.get_project(bad)


class EngineAuthorizationTests(CoolifyCase):
    auto_link = False                     # these tests link through the admin API

    def setUp(self):
        super().setUp()
        self.secret_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.secret_dir, True)
        self.app.config.update(AUTHZ_URL="http://authz.test:8010", AUTHZ_WEBSITE_KEY=WEBSITE_KEY)
        self.configure_coolify()
        self.authz, self.coolify = FakeAuthz(), FakeCoolify()
        self.router = Router(self.authz, self.coolify)

    def linked_project(self, **extra):
        """Create a project as the student, link it as the admin (no deploy yet)."""
        self.as_student()
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            slug = self.create(deploy=False, **extra).get_json()["project"]["slug"]
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": self.csrf()})
        self.login(ADMIN[0], ADMIN[2])
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            r = self.call("post", f"/api/admin/projects/{slug}/authorization",
                          {"authz_project_id": "prj_0123456789abcdef"})
        self.assertEqual(r.status_code, 200, r.get_json())
        return slug

    def deploy(self, slug):
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            dep = self.call("post", f"/api/projects/{slug}/deploy").get_json()["deployment"]
            return self.wait(dep["id"])

    def everything_recorded(self):
        conn = self.app_conn()
        dump = "\n".join(str(tuple(r)) for t in ("deployments", "deploy_logs", "projects", "audit_log",
                                                  "env_vars", "settings")
                         for r in conn.execute(f"SELECT * FROM {t}"))
        return dump + json.dumps([c["json"] for c in self.coolify.calls])

    def test_coolify_builds_without_a_sidecar_and_requests_no_credential(self):
        for sink in ("", "secret-dir"):
            with self.subTest(sink=sink):
                self.app.config["RUNTIME_SECRET_DIR"] = self.secret_dir if sink else ""
                conn = self.app_conn()
                conn.execute("DELETE FROM deployments")
                conn.execute("DELETE FROM projects")
                conn.commit()
                self.authz.calls.clear()
                slug = self.linked_project()
                d = self.deploy(slug)
                self.assertEqual(d["status"], "ready", d)
                self.assertFalse(any(c["path"] == "/deployments" for c in self.authz.calls),
                                 "Coolify cannot isolate a sidecar, so no runtime credential is requested")
                self.assertTrue(any(c["path"].endswith("/repository") for c in self.authz.calls),
                                "the repository is still verified with GitHub through authz")
                self.assertFalse(d["authz_deployment_id"])
                self.assertEqual(os.listdir(self.secret_dir), [])
                self.assertNotIn(RUNTIME_SECRET, self.everything_recorded())

    def test_runtime_secret_is_never_put_in_the_compose_file_or_coolify_envs(self):
        self.app.config["RUNTIME_SECRET_DIR"] = self.secret_dir
        slug = self.linked_project()
        self.assertEqual(self.deploy(slug)["status"], "ready")
        import base64
        create = next(c for c in self.coolify.calls if (c["method"], c["path"]) == ("POST", "/services"))["json"]
        compose = json.loads(base64.b64decode(create["docker_compose_raw"]))
        self.assertEqual(set(compose["services"]), {"app"}, "no sidecar inside a Coolify service")
        self.assertFalse({"CODESKYS_RUNTIME_URL", "CODESKYS_DEPLOYMENT_ID", "CODESKYS_RUNTIME_SECRET"}
                         & set(compose["services"]["app"]["environment"]))
        envs = [e for c in self.coolify.calls if c["path"].endswith("/envs/bulk") for e in c["json"]["data"]]
        self.assertFalse([e for e in envs if e["key"].startswith("CODESKYS_")])

    def test_suspended_project_is_refused_before_anything_is_built(self):
        slug = self.linked_project()
        self.authz.can_deploy = False
        d = self.deploy(slug)
        self.assertEqual(d["status"], "failed")
        self.assertIn("suspended", d["error"])
        self.assertFalse([c for c in self.coolify.calls if c["path"].startswith("/services")],
                         "nothing was created or started")

    def test_repository_mismatch_fails_with_a_clear_message(self):
        slug = self.linked_project()
        self.assertEqual(self.deploy(slug)["status"], "ready")          # registers repository 424242
        self.authz.registered_repo_id = 999999                          # re-registered elsewhere since
        self.coolify.calls.clear()
        d = self.deploy(slug)
        self.assertEqual(d["status"], "failed")
        self.assertIn("registered repository", d["error"])
        self.assertEqual(self.coolify.calls, [], "the live service is not touched")

    def test_authz_unavailable_fails_the_build_honestly(self):
        slug = self.linked_project()
        self.authz.down = True
        d = self.deploy(slug)
        self.assertEqual(d["status"], "failed")
        self.assertIn("temporarily unavailable", d["error"])

    def test_required_mode_refuses_unlinked_projects(self):
        self.app.config["AUTHZ_REQUIRED"] = True
        self.as_student()
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            d = self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(d["status"], "failed")
        self.assertIn("Link this project", d["error"])

    def test_failed_build_revokes_and_removes_a_leftover_credential(self):
        # Coolify builds request no credential; one left on the row (older code) is still revoked.
        self.app.config["RUNTIME_SECRET_DIR"] = self.secret_dir
        slug = self.linked_project()
        self.coolify.app_statuses = ["starting:unhealthy", "exited"]
        real = self.coolify._route
        conn = self.app_conn()

        def route(method, path, body):
            if path.endswith("/start"):
                conn.execute("UPDATE deployments SET authz_deployment_id = 'dep_abcdef0123456789' "
                             "WHERE id = (SELECT MAX(id) FROM deployments)")
                conn.commit()
                runtime_secrets.deliver(self.app.config, "dep_abcdef0123456789", RUNTIME_SECRET)
            return real(method, path, body)
        self.coolify._route = route
        d = self.deploy(slug)
        self.assertEqual(d["status"], "failed")
        self.assertEqual(self.authz.revoked, ["dep_abcdef0123456789"])
        self.assertFalse(os.path.exists(os.path.join(self.secret_dir, "dep_abcdef0123456789")))

    def test_user_cannot_shadow_reserved_runtime_variables(self):
        slug = self.linked_project()
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            self.call("post", f"/api/projects/{slug}/env", {"key": "CODESKYS_RUNTIME_SECRET", "value": "forged"})
        self.deploy(slug)
        sent = [e for c in self.coolify.calls if c["path"].endswith("/envs/bulk") for e in c["json"]["data"]]
        self.assertNotIn("forged", json.dumps(sent))

    def test_delete_revokes_credentials_and_refuses_when_authz_is_down(self):
        self.app.config["RUNTIME_SECRET_DIR"] = self.secret_dir
        slug = self.linked_project()
        self.deploy(slug)
        conn = self.app_conn()                     # a credential left by older code
        conn.execute("UPDATE deployments SET authz_deployment_id = 'dep_abcdef0123456789'")
        conn.commit()
        self.authz.down = True
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            r = self.client.delete(f"/api/projects/{slug}", json={"confirm": slug},
                                   headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.status_code, 503, "must not delete while a credential is still live")
        self.authz.down = False
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            r = self.client.delete(f"/api/projects/{slug}", json={"confirm": slug},
                                   headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.status_code, 200)
        self.assertIn("dep_abcdef0123456789", self.authz.revoked)

    def test_changing_repository_drops_the_verified_id(self):
        slug = self.linked_project()
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            r = self.client.patch(f"/api/projects/{slug}", json={"repo_url": "https://github.com/acme/other"},
                                  headers={"X-CSRF-Token": self.csrf()})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.get_json()["project"]["authorization"]["github_repo_id"])

    def test_admin_link_refuses_a_project_bound_to_another_repository(self):
        self.authz.registered_repo = "https://github.com/someone/else"
        self.as_student()
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            slug = self.create(deploy=False).get_json()["project"]["slug"]
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": self.csrf()})
        self.login(ADMIN[0], ADMIN[2])
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            r = self.call("post", f"/api/admin/projects/{slug}/authorization",
                          {"authz_project_id": "prj_0123456789abcdef"})
        self.assertEqual(r.status_code, 409)

    def test_admin_link_validates_input_and_reports_unknown_projects(self):
        slug = self.linked_project()
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            bad = self.call("post", f"/api/admin/projects/{slug}/authorization", {"authz_project_id": "../x"})
            missing = self.call("post", f"/api/admin/projects/{slug}/authorization",
                                {"authz_project_id": "prj_00000000deadbeef"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(missing.status_code, 404)

    def test_suspend_reaches_authz_and_pauses_locally(self):
        slug = self.linked_project()
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            r = self.call("post", f"/api/admin/projects/{slug}/suspend", {"suspended": True, "reason": "test"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.authz.can_deploy)
        self.assertTrue(self.client.get(f"/api/projects/{slug}").get_json()["project"]["is_paused"])

    def test_repository_picker_is_admin_only_and_filters_fields(self):
        self.linked_project()  # leaves the admin signed in
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            r = self.client.get("/api/admin/github/installations/179150342/repositories")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("extra", r.get_json()["repositories"][0])
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": self.csrf()})
        self.as_student()
        self.assertEqual(self.client.get("/api/admin/github/installations/179150342/repositories").status_code, 403)


class OwnerLinkTests(EngineAuthorizationTests.__base__):
    def setUp(self):
        super().setUp()
        self.app.config.update(AUTHZ_URL="http://authz.test:8010", AUTHZ_WEBSITE_KEY=WEBSITE_KEY)
        self.authz = FakeAuthz()
        self.router = Router(self.authz, FakeCoolify())
        self.as_student()
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            self.slug = self.create(deploy=False).get_json()["project"]["slug"]

    def link(self, credential):
        with mock.patch("kova.engine.requests.request", side_effect=self.router):
            return self.call("post", f"/api/projects/{self.slug}/authorization/link", {"credential": credential})

    def test_wrong_credential_is_refused_in_plain_language(self):
        r = self.link("dpl_live_wrong")
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("EXECUTION CONTEXT", r.get_json()["error"])

    def test_correct_credential_links_and_is_never_stored(self):
        r = self.link(STUDENT_CREDENTIAL)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["github_repo_id"], 424242)
        conn = self.app_conn()
        dump = "\n".join(str(tuple(row)) for t in ("projects", "audit_log", "settings")
                         for row in conn.execute(f"SELECT * FROM {t}"))
        self.assertNotIn(STUDENT_CREDENTIAL, dump)
        self.assertIn("prj_0123456789abcdef", dump)

    def test_guessing_is_rate_limited(self):
        codes = [self.link(f"dpl_live_guess{i}").status_code for i in range(6)]
        self.assertEqual(codes[-1], 429)

    def test_other_users_cannot_link_someone_elses_project(self):
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": self.csrf()})
        from kova.db import connect
        from kova.users import create_user
        conn = connect(self.app.config["DATABASE"])
        create_user(conn, "eve@example.test", "Eve", "eve-password-123456", role="student")
        conn.close()
        self.login("eve@example.test", "eve-password-123456")
        self.assertEqual(self.link(STUDENT_CREDENTIAL).status_code, 404)


class AppControlTests(CoolifyCase):
    # Coolify-backed start/stop/restart: test_coolify.AppControlTests.
    def test_simulated_projects_cannot_be_started_or_stopped(self):
        self.as_student()
        slug = self.create(deploy=False).get_json()["project"]["slug"]
        self.assertEqual(self.call("post", f"/api/projects/{slug}/app/stop").status_code, 409)
        self.assertEqual(self.call("post", f"/api/projects/{slug}/app/explode").status_code, 404)

class RuntimeSecretSinkTests(CoolifyCase):
    def test_refuses_unexpected_ids_and_traversal(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        cfg = {"RUNTIME_SECRET_DIR": d}
        for bad in ("../etc", "dep_../../x", "prj_0123456789", ""):
            with self.assertRaises(runtime_secrets.SinkError):
                runtime_secrets.deliver(cfg, bad, "s")
        p = runtime_secrets.deliver(cfg, "dep_abcdef012345", "s3cret")
        self.assertTrue(os.path.exists(p))
        self.assertTrue(runtime_secrets.discard(cfg, "dep_abcdef012345"))
        self.assertFalse(os.path.exists(os.path.dirname(p)))
        self.assertFalse(runtime_secrets.discard(cfg, "dep_abcdef012345"))

    def test_refuses_platform_secret_groups(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for gid in ("1998", "1999"):
            with self.subTest(gid=gid), self.assertRaises(runtime_secrets.SinkError):
                runtime_secrets.deliver({"RUNTIME_SECRET_DIR": d, "RUNTIME_SECRET_GID": gid},
                                        "dep_abcdef012345", "s3cret")
        self.assertEqual(os.listdir(d), [], "nothing is written with a reserved group")


class CoolifyHardeningTests(CoolifyCase):
    def run_one(self, **project):
        self.as_student()
        fake = FakeCoolify()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            d = self.wait(self.create(**project).get_json()["deployment_id"])
        return d, fake

    def test_apps_go_to_the_dedicated_destination_network(self):
        self.configure_coolify(coolify_destination_uuid="dst-students")
        d, fake = self.run_one()
        create = next(c for c in fake.calls if (c["method"], c["path"]) == ("POST", "/services"))["json"]
        self.assertEqual(create["destination_uuid"], "dst-students")
        self.assertIs(create["connect_to_docker_network"], False, "never joined to the destination network")

    def test_http_only_mode_until_https_exists(self):
        self.configure_coolify(coolify_force_https="0")
        d, fake = self.run_one()
        self.assertEqual(d["status"], "ready", d)
        create = next(c for c in fake.calls if (c["method"], c["path"]) == ("POST", "/services"))["json"]
        self.assertEqual(create["urls"], [{"name": "app", "url": "http://web-app.apps.example.com:3000"}])
        self.assertEqual(d["url"], "http://web-app.apps.example.com")
