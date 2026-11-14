"""Direct file upload: safe unpacking, real hosting, re-upload and expiry."""
import io
import os
import zipfile
from datetime import datetime, timedelta, timezone

from kova import jobs
from kova.db import connect
from kova.uploads import UploadError, unpack
from .base import AppTestCase, STUDENT


def make_zip(files: dict, symlink: str | None = None) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
    buf.seek(0)
    return buf


class UploadTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.login(STUDENT[0], STUDENT[2])

    def drain(self):
        return jobs.drain(self.app.config["DATABASE"], self.app.config)

    def upload(self, data, name="My Site", slug=None, path="/api/projects/upload"):
        form = {"file": (data, "site.zip")}
        if path == "/api/projects/upload":
            form["name"] = name
            if slug:
                form["slug"] = slug
        return self.client.post(path, data=form, content_type="multipart/form-data",
                                headers={"X-CSRF-Token": self.csrf()})

    def deployment(self, dep_id):
        conn = connect(self.app.config["DATABASE"])
        try:
            return conn.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
        finally:
            conn.close()

    # ---- happy path
    def test_zip_becomes_a_real_live_site(self):
        r = self.upload(make_zip({"index.html": "<h1>Hello Codeskys</h1>", "css/a.css": "body{}"}))
        self.assertEqual(r.status_code, 201, r.get_json())
        slug = r.get_json()["project"]["slug"]
        dep_id = r.get_json()["deployment_id"]
        self.assertEqual(self.deployment(dep_id)["status"], "queued")
        self.drain()
        dep = self.deployment(dep_id)
        self.assertEqual(dep["status"], "ready")
        self.assertEqual(dep["provider"], "static")
        self.assertEqual(dep["url"], f"/live/{slug}/")
        page = self.client.get(f"/live/{slug}/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Hello Codeskys", page.data)
        self.assertIn("sandbox", page.headers["Content-Security-Policy"])
        self.assertNotIn("allow-same-origin", page.headers["Content-Security-Policy"])
        self.assertEqual(self.client.get(f"/live/{slug}/css/a.css").status_code, 200)
        self.assertEqual(self.client.get(f"/live/{slug}/missing.html").status_code, 404)

    def test_wrapper_folder_and_dist_folder_are_found(self):
        for files in ({"my-site/index.html": "wrapped"}, {"dist/index.html": "built", "src/a.js": "x"}):
            r = self.upload(make_zip(files), name="Case " + next(iter(files))[:3])
            self.assertEqual(r.status_code, 201, r.get_json())
            self.drain()
            slug = r.get_json()["project"]["slug"]
            conn = connect(self.app.config["DATABASE"])
            conn.execute("UPDATE users SET plan = 'pro' WHERE id = ?", (self.student_id,))
            conn.commit(); conn.close()
            self.assertEqual(self.client.get(f"/live/{slug}/").status_code, 200)

    def test_custom_link_name(self):
        r = self.upload(make_zip({"index.html": "x"}), slug="rahul-portfolio")
        self.assertEqual(r.get_json()["project"]["slug"], "rahul-portfolio")

    def test_reserved_and_bad_link_names_are_refused(self):
        self.assertEqual(self.upload(make_zip({"index.html": "x"}), slug="admin").status_code, 422)
        self.assertEqual(self.upload(make_zip({"index.html": "x"}), slug="Bad Name!").status_code, 422)

    # ---- rejections
    def test_not_a_zip_is_refused(self):
        r = self.upload(io.BytesIO(b"this is not a zip"))
        self.assertEqual(r.status_code, 422)
        self.assertIn("not a valid .zip", r.get_json()["error"])

    def test_zip_without_index_html_fails_with_a_clear_reason(self):
        r = self.upload(make_zip({"readme.txt": "hi"}))
        self.drain()
        dep = self.deployment(r.get_json()["deployment_id"])
        self.assertEqual(dep["status"], "failed")
        self.assertIn("index.html", dep["error"])

    def test_path_traversal_rejects_the_whole_upload(self):
        r = self.upload(make_zip({"index.html": "x", "../evil.txt": "pwned"}))
        self.drain()
        dep = self.deployment(r.get_json()["deployment_id"])
        self.assertEqual(dep["status"], "failed")
        self.assertIn("outside", dep["error"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))

    def test_symlinks_are_rejected(self):
        r = self.upload(make_zip({"index.html": "x"}, symlink="link"))
        self.drain()
        self.assertEqual(self.deployment(r.get_json()["deployment_id"])["status"], "failed")

    def test_secrets_and_junk_are_never_published(self):
        r = self.upload(make_zip({"index.html": "x", ".env": "SECRET=1", ".envrc": "export K=1", ".git/config": "c",
                                  "node_modules/a/b.js": "j", "run.exe": "MZ", "ok.js": "1"}))
        self.drain()
        slug = r.get_json()["project"]["slug"]
        self.assertEqual(self.deployment(r.get_json()["deployment_id"])["status"], "ready")
        self.assertEqual(self.client.get(f"/live/{slug}/ok.js").status_code, 200)
        for hidden in (".env", ".envrc", ".git/config", "node_modules/a/b.js", "run.exe"):
            self.assertEqual(self.client.get(f"/live/{slug}/{hidden}").status_code, 404, hidden)

    def test_size_and_file_count_limits(self):
        with self.assertRaises(UploadError):
            unpack_to = os.path.join(self.tmp, "u1")
            p = os.path.join(self.tmp, "big.zip")
            open(p, "wb").write(make_zip({"index.html": "x" * 5000}).getvalue())
            from pathlib import Path
            unpack(Path(p), Path(unpack_to), max_bytes=1000, max_files=10)
        with self.assertRaises(UploadError):
            p = os.path.join(self.tmp, "many.zip")
            open(p, "wb").write(make_zip({f"f{i}.txt": "x" for i in range(20)} | {"index.html": "x"}).getvalue())
            from pathlib import Path
            unpack(Path(p), Path(os.path.join(self.tmp, "u2")), max_bytes=10**6, max_files=5)

    # ---- re-upload and expiry
    def test_reupload_updates_live_site_without_extending_free_window(self):
        r = self.upload(make_zip({"index.html": "version one"}))
        slug = r.get_json()["project"]["slug"]
        self.drain()
        first = self.deployment(r.get_json()["deployment_id"])
        r2 = self.upload(make_zip({"index.html": "version two"}), path=f"/api/projects/{slug}/upload")
        self.assertEqual(r2.status_code, 201, r2.get_json())
        self.drain()
        second = self.deployment(r2.get_json()["deployment"]["id"])
        self.assertEqual(second["status"], "ready")
        self.assertIn(b"version two", self.client.get(f"/live/{slug}/").data)
        self.assertEqual(second["expires_at"], first["expires_at"], "re-upload must not extend the free 24 hours")
        self.assertEqual(self.deployment(first["id"])["source_dir"], "", "old files should be removed")

    def test_expired_site_shows_404_and_files_are_removed(self):
        r = self.upload(make_zip({"index.html": "soon gone"}))
        slug = r.get_json()["project"]["slug"]
        dep_id = r.get_json()["deployment_id"]
        self.drain()
        self.assertEqual(self.client.get(f"/live/{slug}/").status_code, 200)
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        conn = connect(self.app.config["DATABASE"])
        conn.execute("UPDATE deployments SET expires_at = ? WHERE id = ?", (past, dep_id))
        conn.commit(); conn.close()
        self.assertEqual(self.client.get(f"/live/{slug}/").status_code, 404, "404 even before the sweeper runs")
        conn = connect(self.app.config["DATABASE"])
        jobs.sweep(conn, self.app.config); conn.close()
        self.drain()
        dep = self.deployment(dep_id)
        self.assertEqual(dep["status"], "expired")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "uploads", str(dep["project_id"]), str(dep_id))))

    def test_git_projects_cannot_take_uploads(self):
        r = self.post("/api/projects", {"name": "g", "repo_url": "https://github.com/a/b", "deploy": False})
        self.assertEqual(r.status_code, 201, r.get_json())
        r2 = self.upload(make_zip({"index.html": "x"}), path=f"/api/projects/{r.get_json()['project']['slug']}/upload")
        self.assertEqual(r2.status_code, 409)

    def test_other_users_cannot_upload_to_my_project(self):
        r = self.upload(make_zip({"index.html": "x"}))
        slug = r.get_json()["project"]["slug"]
        other = self.app.test_client()
        conn = connect(self.app.config["DATABASE"])
        from kova.users import create_user
        create_user(conn, "o@example.test", "Other", "other-pass-123456", role="student"); conn.close()
        self.login("o@example.test", "other-pass-123456", client=other)
        tok = self.csrf(other)
        r2 = other.post(f"/api/projects/{slug}/upload", data={"file": (make_zip({"index.html": "y"}), "s.zip")},
                        content_type="multipart/form-data", headers={"X-CSRF-Token": tok})
        self.assertEqual(r2.status_code, 404)

    # ---- real subdomain links
    def test_wildcard_subdomain_serves_the_site_and_nothing_else(self):
        self.app.config["SITES_DOMAIN"] = "apps.example.test"
        r = self.upload(make_zip({"index.html": "on a subdomain"}), slug="demo-one")
        self.drain()
        dep = self.deployment(r.get_json()["deployment_id"])
        self.assertEqual(dep["url"], "http://demo-one.apps.example.test/")
        page = self.client.get("/", headers={"Host": "demo-one.apps.example.test"})
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"on a subdomain", page.data)
        self.assertIn("allow-same-origin", page.headers["Content-Security-Policy"])
        api = self.client.get("/api/auth/me", headers={"Host": "demo-one.apps.example.test"})
        self.assertNotIn(b"csrf", api.data, "the dashboard API must not answer on a site subdomain")
        self.assertEqual(self.client.get("/", headers={"Host": "nobody.apps.example.test"}).status_code, 404)

    # ---- disk cleanup of deployments that will never serve
    def test_cancelled_upload_leaves_no_files(self):
        from unittest import mock
        from kova import engine, uploads

        def cancel_midway(zip_path, dest, **kw):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "index.html").write_text("half")
            raise engine.Cancelled()
        with mock.patch("kova.uploads.unpack", side_effect=cancel_midway):
            r = self.upload(make_zip({"index.html": "x"}))
            self.drain()
        dep = self.deployment(r.get_json()["deployment_id"])
        self.assertEqual(dep["status"], "cancelled")
        self.assertFalse(uploads.site_dir(self.app.config, dep["project_id"], dep["id"]).exists())
        self.assertFalse(uploads.staged_zip(self.app.config, dep["project_id"], dep["id"]).exists())

    def test_sweep_frees_leftovers_but_keeps_the_live_site(self):
        import time as _time
        from kova import uploads
        r = self.upload(make_zip({"index.html": "live one"}))
        self.drain()
        live = self.deployment(r.get_json()["deployment_id"])
        conn = connect(self.app.config["DATABASE"])
        self.addCleanup(conn.close)
        # A failed deployment whose files were left behind (e.g. the worker died mid-build).
        cur = conn.execute("INSERT INTO deployments (project_id, number, status, stage, target, branch, provider, created_at) "
                           "VALUES (?, 99, 'failed', 'failed', 'production', 'upload', 'static', ?)",
                           (live["project_id"], live["created_at"]))
        conn.commit()
        stale = uploads.site_dir(self.app.config, live["project_id"], cur.lastrowid)
        stale.mkdir(parents=True)
        (stale / "index.html").write_text("old")
        incoming = uploads.upload_root(self.app.config) / "_incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        old, fresh = incoming / "old.zip", incoming / "fresh.zip"
        old.write_bytes(b"x"); fresh.write_bytes(b"y")
        past = _time.time() - 2 * uploads.INCOMING_MAX_AGE
        os.utime(old, (past, past))
        jobs.sweep(conn, self.app.config)
        jobs.sweep(conn, self.app.config)          # idempotent
        self.assertFalse(stale.exists())
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists(), "an upload still in flight is kept")
        self.assertEqual(self.client.get(f"/live/{r.get_json()['project']['slug']}/").status_code, 200)

    def test_the_same_file_sent_twice_is_one_deployment(self):
        r = self.upload(make_zip({"index.html": "v1"}))
        self.drain()
        slug = r.get_json()["project"]["slug"]
        data = make_zip({"index.html": "v2"}).getvalue()
        import io as _io
        a = self.upload(_io.BytesIO(data), path=f"/api/projects/{slug}/upload").get_json()["deployment"]["id"]
        b = self.upload(_io.BytesIO(data), path=f"/api/projects/{slug}/upload").get_json()["deployment"]["id"]
        self.assertEqual(a, b)
        self.drain()
        self.assertIn(b"v2", self.client.get(f"/live/{slug}/").data)
