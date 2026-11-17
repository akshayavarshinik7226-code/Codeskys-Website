"""Batch A rules: the free plan's single 24-hour clock, paid plans, renaming a project's link,
and the history rules (only the latest deployment can be acted on)."""
import io
import zipfile
from datetime import datetime, timedelta, timezone

from kova import jobs
from kova.db import connect, utcnow
from .base import ADMIN, STUDENT, AppTestCase
from .test_platform import PlatformCase, REPO


def iso(hours=0, minutes=0):
    return (datetime.now(timezone.utc) + timedelta(hours=hours, minutes=minutes)).isoformat(timespec="seconds")


def make_zip(html="<h1>Hello</h1>"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("index.html", html)
    buf.seek(0)
    return buf


class _Helpers(PlatformCase):
    def db(self):
        return self.app_conn()

    def upload(self, name="My Site", slug=None, path="/api/projects/upload", html="<h1>Hi</h1>"):
        form = {"file": (make_zip(html), "site.zip")}
        if path == "/api/projects/upload":
            form["name"] = name
            if slug:
                form["slug"] = slug
        return self.client.post(path, data=form, content_type="multipart/form-data",
                                headers={"X-CSRF-Token": self.csrf()})

    def first_upload(self):
        self.as_student()
        r = self.upload(slug="clock-site")
        self.assertEqual(r.status_code, 201, r.get_json())
        self.work()
        return "clock-site"


class FreeClockTests(_Helpers):
    """Free plan: one project, one 24-hour window, no new clock on redeploy, nothing after it ends."""

    def test_first_deploy_starts_a_24_hour_window(self):
        slug = self.first_upload()
        p = self.client.get(f"/api/projects/{slug}").get_json()["project"]
        exp = datetime.fromisoformat(p["access"]["expires_at"])
        hours = (exp - datetime.now(timezone.utc)).total_seconds() / 3600
        self.assertTrue(23.9 < hours <= 24.01, hours)
        self.assertTrue(p["access"]["is_free"])
        self.assertFalse(p["access"]["blocked"])

    def test_every_redeploy_keeps_the_same_expiry(self):
        slug = self.first_upload()
        clock = self.client.get(f"/api/projects/{slug}").get_json()["project"]["access"]["expires_at"]
        for _ in range(3):
            r = self.upload(path=f"/api/projects/{slug}/upload", html="<h1>v</h1>")
            self.assertEqual(r.status_code, 201, r.get_json())
            self.work()
        deps = self.client.get(f"/api/projects/{slug}/deployments").get_json()["deployments"]
        self.assertEqual(len(deps), 4)
        self.assertEqual({d["expires_at"] for d in deps}, {clock})   # history shows ONE expiry
        conn = self.db()
        stored = {r["expires_at"] for r in conn.execute("SELECT expires_at FROM deployments WHERE status='ready'")}
        self.assertEqual(stored, {clock})

    def test_git_project_redeploys_share_one_expiry_too(self):
        self.as_student()
        first = self.wait(self.create().get_json()["deployment_id"])
        second = self.wait(self.call("post", "/api/projects/web-app/deploy").get_json()["deployment"]["id"])
        self.assertEqual(first["expires_at"], second["expires_at"])

    def test_after_expiry_the_link_is_404_and_redeploy_is_blocked(self):
        slug = self.first_upload()
        self.assertEqual(self.client.get(f"/live/{slug}/").status_code, 200)
        conn = self.db()
        past = iso(minutes=-5)
        conn.execute("UPDATE projects SET free_expires_at = ?", (past,))
        conn.execute("UPDATE deployments SET expires_at = ?", (past,))
        conn.commit()
        self.assertEqual(self.client.get(f"/live/{slug}/").status_code, 404)
        jobs.sweep(conn, self.app.config)
        self.work()
        r = self.upload(path=f"/api/projects/{slug}/upload")
        self.assertEqual(r.status_code, 402)
        self.assertIn("Upgrade", r.get_json()["error"])
        p = self.client.get(f"/api/projects/{slug}").get_json()["project"]
        self.assertTrue(p["access"]["expired"] and p["access"]["blocked"])
        self.assertEqual(self.client.get(f"/live/{slug}/").status_code, 404)

    def test_git_redeploy_after_expiry_is_blocked(self):
        self.as_student()
        self.wait(self.create().get_json()["deployment_id"])
        conn = self.db()
        conn.execute("UPDATE projects SET free_expires_at = ?", (iso(minutes=-1),))
        conn.commit()
        r = self.call("post", "/api/projects/web-app/deploy")
        self.assertEqual(r.status_code, 402)
        self.assertIn("free 24 hours", r.get_json()["error"])

    def test_deleting_an_expired_project_does_not_give_a_second_free_window(self):
        slug = self.first_upload()
        conn = self.db()
        conn.execute("UPDATE projects SET free_expires_at = ?", (iso(minutes=-1),))
        conn.commit()
        self.assertEqual(self.call("delete", f"/api/projects/{slug}", {"confirm": slug}).status_code, 200)
        r = self.upload(name="Another", slug="another-one")
        self.assertEqual(r.status_code, 402)
        self.assertIn("already used", r.get_json()["error"])
        r = self.create(deploy=False)
        self.assertEqual(r.status_code, 402)

    def test_a_project_that_never_went_live_does_not_use_up_the_window(self):
        self.as_student()
        self.assertEqual(self.create(deploy=False).status_code, 201)
        self.assertEqual(self.call("delete", "/api/projects/web-app", {"confirm": "web-app"}).status_code, 200)
        self.assertEqual(self.create(deploy=False).status_code, 201)

    def test_students_cannot_lengthen_the_free_window(self):
        self.as_student()
        self.create(deploy=False)
        r = self.call("patch", "/api/projects/web-app", {"ttl_hours": 720})
        self.assertEqual(r.status_code, 403)

    def test_upload_limit_default_is_100_mb(self):
        import os
        from unittest import mock
        from kova.config import build_config, ROOT
        with mock.patch.dict(os.environ):
            os.environ.pop("KOVA_MAX_UPLOAD_MB", None)
            self.assertEqual(build_config()["MAX_UPLOAD_MB"], 100)
        self.assertIn("KOVA_MAX_UPLOAD_MB=100", (ROOT / ".env.example").read_text())


class PaidPlanTests(_Helpers):
    def pay(self, plan, days):   # an older administrator/plan-based paid plan (still supported)
        conn = self.db()
        until = None if days is None else iso(hours=24 * days)
        conn.execute("UPDATE users SET plan = ?, plan_expires_at = ? WHERE id = ?", (plan, until, self.student_id))
        conn.commit()
        from kova import plans
        plans.apply_plan_to_live_projects(conn, self.student_id)

    def test_upgrading_extends_what_is_live_to_the_plan_length(self):
        slug = self.first_upload()
        self.pay("pro", 180)
        dep = self.client.get(f"/api/projects/{slug}").get_json()["project"]["production"]
        left = (datetime.fromisoformat(dep["expires_at"]) - datetime.now(timezone.utc)).days
        self.assertTrue(178 <= left <= 180, left)

    def test_paid_projects_get_the_plan_expiry_not_24_hours(self):
        self.as_student()
        self.pay("pro", 180)
        r = self.upload(slug="paid-site")
        self.assertEqual(r.status_code, 201)
        self.work()
        p = self.client.get("/api/projects/paid-site").get_json()["project"]
        left = (datetime.fromisoformat(p["production"]["expires_at"]) - datetime.now(timezone.utc)).days
        self.assertTrue(178 <= left <= 180, left)
        self.assertFalse(p["access"]["is_free"])

    def hosting(self, days):
        """Give the student paid hosting (what a Rs 510 / Rs 1,599 payment does); days=None -> lifetime."""
        from kova import engine
        conn = self.db()
        until = "2126-01-01T00:00:00+00:00" if days is None else iso(hours=24 * days)
        conn.execute("UPDATE users SET hosting_until = ? WHERE id = ?", (until, self.student_id))
        conn.commit()
        engine.extend_for_hosting(conn, self.student_id)
        conn.commit()

    def test_paid_hosting_lasts_until_the_paid_end_plus_grace_and_allows_many_projects(self):
        self.as_student()
        self.hosting(180)
        self.assertEqual(self.upload(slug="hosted-one").status_code, 201)
        self.work()
        p = self.client.get("/api/projects/hosted-one").get_json()["project"]
        left = (datetime.fromisoformat(p["production"]["expires_at"]) - datetime.now(timezone.utc)).days
        self.assertTrue(179 <= left <= 181, left)           # 180 days + 24 h grace
        self.assertFalse(p["access"]["is_free"])
        self.assertEqual(self.upload(name="Second", slug="second-one").status_code, 201)   # many projects

    def test_lifetime_hosting_has_no_end_date(self):
        self.as_student()
        self.hosting(None)
        self.assertEqual(self.upload(slug="forever").status_code, 201)
        self.work()
        p = self.client.get("/api/projects/forever").get_json()["project"]
        self.assertTrue(p["access"]["lifetime"])
        self.assertIsNone(p["access"]["expires_at"])
        self.assertEqual(self.upload(name="Second", slug="second-one").status_code, 201)

    def test_buying_hosting_extends_a_project_that_is_already_live_on_the_free_plan(self):
        slug = self.first_upload()
        free_end = self.client.get(f"/api/projects/{slug}").get_json()["project"]["production"]["expires_at"]
        self.hosting(180)
        dep = self.client.get(f"/api/projects/{slug}").get_json()["project"]["production"]
        self.assertGreater(dep["expires_at"], free_end)

    def test_after_paid_hosting_and_grace_end_the_project_is_blocked_with_a_renew_message(self):
        self.as_student()
        self.hosting(180)
        self.upload(slug="was-hosted")
        self.work()
        conn = self.db()      # hosting ended more than 24 h ago
        conn.execute("UPDATE users SET hosting_until = ? WHERE id = ?", (iso(hours=-30), self.student_id))
        conn.commit()
        r = self.upload(path="/api/projects/was-hosted/upload")
        self.assertEqual(r.status_code, 402)
        self.assertIn("Renew", r.get_json()["error"])

    def test_paid_user_can_redeploy_a_project_that_expired_on_the_free_plan(self):
        slug = self.first_upload()
        conn = self.db()
        conn.execute("UPDATE projects SET free_expires_at = ?", (iso(minutes=-1),))
        conn.commit()
        self.assertEqual(self.upload(path=f"/api/projects/{slug}/upload").status_code, 402)
        self.pay("pro", 180)
        self.assertEqual(self.upload(path=f"/api/projects/{slug}/upload").status_code, 201)
        self.work()
        self.assertEqual(self.client.get(f"/live/{slug}/").status_code, 200)

    def test_lapsed_paid_plan_does_not_restart_the_free_clock(self):
        self.as_student()
        self.pay("pro", 180)
        self.upload(slug="was-paid")
        self.work()
        conn = self.db()   # the plan ends: back to the free plan with no free clock on this project
        conn.execute("UPDATE users SET plan = 'hobby', plan_expires_at = NULL WHERE id = ?", (self.student_id,))
        conn.commit()
        r = self.upload(path="/api/projects/was-paid/upload")
        self.assertEqual(r.status_code, 402)


class BillingCatalogTests(AppTestCase):
    def test_default_prices(self):
        from kova.billing import DEFAULT_CATALOG
        self.assertEqual(DEFAULT_CATALOG["hosting-6m"]["amount"], 51000)       # Rs 510
        self.assertEqual(DEFAULT_CATALOG["hosting-6m"]["period_days"], 180)
        self.assertEqual(DEFAULT_CATALOG["hosting-lifetime"]["amount"], 159900)  # Rs 1599
        self.assertEqual(DEFAULT_CATALOG["hosting-lifetime"]["grants_plan"], "hosting")


class RenameTests(PlatformCase):
    def setUp(self):
        super().setUp()
        self.as_student()

    def test_renaming_changes_the_name_and_the_live_link(self):
        slug = "rag-company-know-chat"
        self.assertEqual(self.create(name="RAG Company Know Chat", slug=slug).status_code, 201)
        self.work()
        old = self.client.get(f"/sites/{slug}/")
        self.assertEqual(old.status_code, 200)
        r = self.call("patch", f"/api/projects/{slug}", {"name": "Asha RAG Project", "slug": "asha-rag-project"})
        self.assertEqual(r.status_code, 200, r.get_json())
        p = r.get_json()["project"]
        self.assertEqual((p["name"], p["slug"]), ("Asha RAG Project", "asha-rag-project"))
        self.assertEqual(self.client.get("/sites/asha-rag-project/").status_code, 200)
        self.assertEqual(self.client.get(f"/sites/{slug}/").status_code, 404)    # the old link stops working
        self.assertEqual(self.client.get(f"/api/projects/{slug}").status_code, 404)
        self.assertEqual(p["production"]["url"], "/sites/asha-rag-project/")
        self.assertEqual(p["primary_domain"], "asha-rag-project.codeskys.com")
        deps = self.client.get("/api/projects/asha-rag-project/deployments").get_json()["deployments"]
        self.assertTrue(all("asha-rag-project" in d["url"] for d in deps))

    def test_description_is_separate_and_does_not_touch_the_link(self):
        self.create(deploy=False)
        r = self.call("patch", "/api/projects/web-app", {"description": "My notes"})
        self.assertEqual(r.get_json()["project"]["slug"], "web-app")
        self.assertEqual(r.get_json()["project"]["description"], "My notes")

    def test_name_taken_reserved_and_invalid_are_refused(self):
        self.create(deploy=False)
        self.upgrade_student()
        self.create(name="Other One", slug="other-one", deploy=False)
        for bad, code in (("other-one", 409), ("admin", 422), ("ab", 422), ("Bad Name!", 422), ("a--b", 422)):
            r = self.call("patch", "/api/projects/web-app", {"slug": bad})
            self.assertEqual(r.status_code, code, bad)
        self.assertEqual(self.client.get("/api/projects/web-app").status_code, 200)   # nothing changed

    def test_link_check_says_free_taken_or_same(self):
        self.create(deploy=False)
        self.upgrade_student()
        self.create(name="Other One", slug="other-one", deploy=False)
        ask = lambda n: self.client.get("/api/projects/web-app/link-check", query_string={"name": n}).get_json()
        free = ask("Brand New Name")
        self.assertEqual((free["slug"], free["available"], free["same"]), ("brand-new-name", True, False))
        self.assertFalse(ask("Other One")["available"])
        same = ask("Web App")
        self.assertTrue(same["available"] and same["same"])
        self.assertFalse(ask("Admin")["available"])
        self.assertFalse(ask("")["available"])

    def test_another_user_cannot_rename_or_probe_my_project(self):
        self.create(deploy=False)
        other = self.app.test_client()
        self.login(ADMIN[0], ADMIN[2], client=other)   # admin can; a different student cannot
        conn = self.app_conn()
        from kova.users import create_user
        create_user(conn, "eve@gmail.com", "Eve", "Maple-River-Lamp-47!", "student")
        eve = self.app.test_client()
        self.login("eve@gmail.com", "Maple-River-Lamp-47!", client=eve)
        tok = eve.get("/api/auth/me").get_json()["csrf"]
        r = eve.patch("/api/projects/web-app", json={"slug": "stolen-name"}, headers={"X-CSRF-Token": tok})
        self.assertEqual(r.status_code, 404)


class HistoryRuleTests(PlatformCase):
    def setUp(self):
        super().setUp()
        self.as_student()

    def test_only_the_latest_deployment_is_flagged_and_actionable(self):
        first = self.wait(self.create().get_json()["deployment_id"])
        second = self.wait(self.call("post", "/api/projects/web-app/deploy").get_json()["deployment"]["id"])
        deps = self.client.get("/api/projects/web-app/deployments").get_json()["deployments"]
        self.assertEqual([d["is_latest"] for d in deps], [True, False])
        self.assertEqual(self.call("post", f"/api/deployments/{first['id']}/redeploy").status_code, 409)
        self.assertEqual(self.call("post", f"/api/deployments/{second['id']}/redeploy").status_code, 201)
        allp = self.client.get("/api/deployments").get_json()["deployments"]
        self.assertEqual(sum(1 for d in allp if d["is_latest"]), 1)

    def test_redeploy_uses_the_projects_current_branch_and_the_newest_commit(self):
        self.app.config["GIT_LOOKUPS"] = False
        first = self.wait(self.create().get_json()["deployment_id"])
        self.call("patch", "/api/projects/web-app", {"branch": "release"})
        new = self.wait(self.call("post", f"/api/deployments/{first['id']}/redeploy").get_json()["deployment"]["id"])
        self.assertEqual(new["branch"], "release")                    # platform settings are included
        self.assertNotEqual(new["commit_sha"], first["commit_sha"])   # looked up fresh, not the old commit


class AdminExemptTests(_Helpers):
    """An administrator account is for testing: no expiry, no free-plan blocks."""

    def test_admin_deployments_never_expire_and_are_never_blocked(self):
        self.login(ADMIN[0], ADMIN[2])
        r = self.upload(slug="admin-site")
        self.assertEqual(r.status_code, 201, r.get_json())
        self.work()
        p = self.client.get("/api/projects/admin-site").get_json()["project"]
        self.assertTrue(p["access"]["admin"])
        self.assertIsNone(p["production"]["expires_at"])
        # even a (wrongly) stored past clock does not block the admin
        conn = self.db()
        conn.execute("UPDATE projects SET free_expires_at = ?", (iso(minutes=-5),))
        conn.commit()
        self.assertEqual(self.upload(path="/api/projects/admin-site/upload").status_code, 201)
        # and an admin can keep creating projects after deleting one
        self.assertEqual(self.call("delete", "/api/projects/admin-site", {"confirm": "admin-site"}).status_code, 200)
        self.assertEqual(self.upload(name="Again", slug="admin-again").status_code, 201)
