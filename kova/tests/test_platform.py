import time

from kova import jobs

from .base import ADMIN, STUDENT, AppTestCase

REPO = "https://github.com/acme/web-app"


class PlatformCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.app.config.update(SIM_SPEED=0.0, GIT_LOOKUPS=False, SYNC_DEPLOYS=True)

    def as_student(self):
        self.login(STUDENT[0], STUDENT[2])

    def call(self, method, path, payload=None):
        token = self.csrf()
        return getattr(self.client, method)(path, json=payload or {}, headers={"X-CSRF-Token": token})

    def create(self, **extra):
        body = {"name": "Web App", "repo_url": REPO, "framework": "react"} | extra
        return self.call("post", "/api/projects", body)

    def upgrade_student(self, plan="pro"):
        """Hobby allows one project; tests that need several use a paid plan."""
        from kova.db import connect
        conn = connect(self.app.config["DATABASE"])
        conn.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, self.student_id))
        conn.commit()
        conn.close()

    def app_conn(self):
        """A direct connection, the way a worker process would open one."""
        from kova.db import connect
        conn = connect(self.app.config["DATABASE"])
        self.addCleanup(conn.close)
        return conn

    def work(self, limit=50):
        """Drain the durable queue the way a worker process would."""
        return jobs.drain(self.app.config["DATABASE"], self.app.config, limit=limit)

    def wait(self, dep_id, timeout=10):
        self.work()
        end = time.time() + timeout
        while time.time() < end:
            d = self.client.get(f"/api/deployments/{dep_id}").get_json()["deployment"]
            if d["status"] not in ("queued", "building"):
                return d
            self.work()
            time.sleep(0.05)
        self.fail("deployment did not finish")


class ProjectTests(PlatformCase):
    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/projects").status_code, 401)

    def test_create_deploys_and_goes_live(self):
        self.as_student()
        r = self.create(env=[{"key": "API_URL", "value": "https://x"}])
        self.assertEqual(r.status_code, 201, r.get_json())
        data = r.get_json()
        self.assertEqual(data["project"]["slug"], "web-app")
        d = self.wait(data["deployment_id"])
        self.assertEqual(d["status"], "ready")
        self.assertEqual(d["url"], "/sites/web-app/")
        proj = self.client.get("/api/projects/web-app").get_json()["project"]
        self.assertEqual(proj["production"]["id"], d["id"])
        site = self.client.get("/sites/web-app/")
        self.assertEqual(site.status_code, 200)
        self.assertIn(b"Web App", site.data)
        logs = self.client.get(f"/api/deployments/{d['id']}").get_json()["logs"]
        self.assertTrue(any("Would clone" in l["line"] for l in logs))
        self.assertTrue(any("1 environment variable" in l["line"] for l in logs))
        # The simulator must announce itself; it is never passed off as a real build.
        self.assertTrue(any("SIMULATOR" in l["line"] for l in logs))
        self.assertTrue(d["is_simulated"])
        notes = self.client.get("/api/notifications").get_json()
        self.assertEqual(notes["unread"], 1)

    def test_invalid_repo_rejected(self):
        self.as_student()
        self.assertEqual(self.create(repo_url="not a repo").status_code, 422)

    def test_hobby_plan_allows_one_project(self):
        self.as_student()
        self.create(deploy=False)
        self.assertEqual(self.create(deploy=False).status_code, 402)

    def test_slug_is_unique(self):
        self.upgrade_student()
        self.as_student()
        self.create(deploy=False)
        r = self.create(deploy=False)
        self.assertEqual(r.get_json()["project"]["slug"], "web-app-2")

    def test_failed_build(self):
        self.as_student()
        r = self.create(build_command="npm run build && exit 1")
        d = self.wait(r.get_json()["deployment_id"])
        self.assertEqual(d["status"], "failed")
        self.assertIn("exit", d["error"])

    def test_students_cannot_see_each_others_projects(self):
        self.login(ADMIN[0], ADMIN[2])
        self.create(deploy=False)
        self.post("/api/auth/logout")
        self.as_student()
        self.assertEqual(self.client.get("/api/projects/web-app").status_code, 404)
        self.assertEqual(self.client.get("/api/projects").get_json()["projects"], [])

    def test_admin_sees_all(self):
        self.as_student()
        self.create(deploy=False)
        self.post("/api/auth/logout")
        self.login(ADMIN[0], ADMIN[2])
        self.assertEqual(len(self.client.get("/api/projects").get_json()["projects"]), 1)

    def test_update_and_pause(self):
        self.as_student()
        self.create(deploy=False)
        r = self.call("patch", "/api/projects/web-app", {"build_command": "vite build", "is_paused": True})
        self.assertEqual(r.get_json()["project"]["build_command"], "vite build")
        self.assertEqual(self.call("post", "/api/projects/web-app/deploy").status_code, 409)

    def test_delete_requires_confirmation(self):
        self.as_student()
        self.create(deploy=False)
        self.assertEqual(self.call("delete", "/api/projects/web-app", {"confirm": "nope"}).status_code, 400)
        self.assertEqual(self.call("delete", "/api/projects/web-app", {"confirm": "web-app"}).status_code, 200)
        self.assertEqual(self.client.get("/api/projects/web-app").status_code, 404)


class DeploymentTests(PlatformCase):
    def test_redeploy_and_instant_rollback(self):
        self.as_student()
        first = self.wait(self.create().get_json()["deployment_id"])
        r = self.call("post", "/api/projects/web-app/deploy")
        second = self.wait(r.get_json()["deployment"]["id"])
        self.assertEqual(second["number"], 2)
        r = self.call("post", f"/api/deployments/{first['id']}/promote")
        self.assertEqual(r.get_json()["mode"], "instant")
        proj = self.client.get("/api/projects/web-app").get_json()["project"]
        self.assertEqual(proj["production"]["id"], first["id"])

    def test_preview_does_not_replace_production(self):
        self.as_student()
        first = self.wait(self.create().get_json()["deployment_id"])
        r = self.call("post", "/api/projects/web-app/deploy", {"target": "preview", "branch": "feature"})
        prev = self.wait(r.get_json()["deployment"]["id"])
        self.assertEqual(prev["status"], "ready")
        proj = self.client.get("/api/projects/web-app").get_json()["project"]
        self.assertEqual(proj["production"]["id"], first["id"])

    def test_deploy_hook(self):
        self.as_student()
        proj = self.create(deploy=False).get_json()["project"]
        token = proj["hook_url"].rsplit("/", 1)[1]
        anon = self.app.test_client()
        r = anon.post(f"/api/hooks/deploy/{token}")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(anon.post("/api/hooks/deploy/" + "x" * 30).status_code, 404)

    def test_cannot_cancel_finished(self):
        self.as_student()
        d = self.wait(self.create().get_json()["deployment_id"])
        self.assertEqual(self.call("post", f"/api/deployments/{d['id']}/cancel").status_code, 409)

    def test_overview_and_analytics(self):
        self.as_student()
        self.wait(self.create().get_json()["deployment_id"])
        o = self.client.get("/api/overview").get_json()
        self.assertEqual(o["stats"]["projects"], 1)
        self.assertEqual(o["stats"]["success_rate"], 100.0)
        a = self.client.get("/api/analytics").get_json()
        self.assertEqual(a["total"], 1)


class EnvAndDomainTests(PlatformCase):
    def test_env_secret_masked_and_revealed(self):
        self.as_student()
        self.create(deploy=False)
        r = self.call("post", "/api/projects/web-app/env", {"items": [{"key": "DB_URL", "value": "postgres://s"},
                                                                       {"key": "PUBLIC", "value": "1", "is_secret": False}]})
        env = {e["key"]: e for e in r.get_json()["env"]}
        self.assertIsNone(env["DB_URL"]["value"])
        self.assertEqual(env["PUBLIC"]["value"], "1")
        rev = self.client.get(f"/api/projects/web-app/env/{env['DB_URL']['id']}/reveal").get_json()
        self.assertEqual(rev["env"]["value"], "postgres://s")
        self.assertEqual(self.call("post", "/api/projects/web-app/env", {"key": "1BAD", "value": "x"}).status_code, 400)

    def test_domains(self):
        self.as_student()
        self.create(deploy=False)
        doms = self.client.get("/api/projects/web-app/domains").get_json()["domains"]
        self.assertTrue(doms[0]["is_system"])
        r = self.call("post", "/api/projects/web-app/domains", {"hostname": "www.Example.com"})
        added = [d for d in r.get_json()["domains"] if not d["is_system"]][0]
        self.assertEqual(added["status"], "pending")
        self.assertEqual(self.call("post", f"/api/projects/web-app/domains/{added['id']}/primary").status_code, 409)
        self.assertEqual(self.call("delete", f"/api/projects/web-app/domains/{doms[0]['id']}").status_code, 409)
        self.assertEqual(self.call("post", "/api/projects/web-app/domains", {"hostname": "bad"}).status_code, 400)


class AccountAndAdminTests(PlatformCase):
    def test_api_token_auth(self):
        self.as_student()
        raw = self.call("post", "/api/tokens", {"name": "CI"}).get_json()["token"]
        anon = self.app.test_client()
        r = anon.get("/api/projects", headers={"Authorization": f"Bearer {raw}"})
        self.assertEqual(r.status_code, 200)
        r = anon.post("/api/projects", json={"repo_url": REPO, "deploy": False},
                      headers={"Authorization": f"Bearer {raw}"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(anon.get("/api/projects", headers={"Authorization": "Bearer kova_wrong"}).status_code, 401)

    def test_password_change(self):
        self.as_student()
        self.assertEqual(self.call("post", "/api/account/password", {"current": "wrong", "new": "x" * 14}).status_code, 400)
        self.assertEqual(self.call("post", "/api/account/password", {"current": STUDENT[2], "new": "brand-new-pass-1"}).status_code, 200)

    def test_admin_only_endpoints(self):
        self.as_student()
        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/integrations").status_code, 403)

    def test_admin_creates_member_and_configures_coolify(self):
        self.login(ADMIN[0], ADMIN[2])
        r = self.call("post", "/api/admin/users", {"email": "new@x.test", "name": "New", "role": "student"})
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.get_json()["temporary_password"])
        r = self.call("post", "/api/admin/integrations/coolify", {"url": "https://coolify.example.com", "token": "abc12345",
                                                                   "project_uuid": "p", "server_uuid": "s",
                                                                   "provider": "coolify"})
        view = r.get_json()
        self.assertEqual(view["active_provider"], "coolify")
        self.assertNotIn("abc12345", r.get_data(as_text=True))
        status = self.client.get("/api/platform/status").get_json()
        self.assertEqual(status["provider"], "coolify")
