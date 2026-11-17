"""Uploaded applications (not finished sites) built by the deployment engine.

The zip is unpacked with the upload rules, packed as a build context and fetched by the
build server from /internal/build-source/...; everything after that is the GitHub path.
"""
import io
import json
import re
import tarfile
from unittest import mock

from kova import composegen
from kova.db import connect
from .test_coolify import CoolifyCase, FakeCoolify, compose_sent, paths
from .test_uploads import make_zip

SOURCE_URL = "http://172.23.0.11:8000"
FLASK = {"app.py": "from flask import Flask\napp = Flask(__name__)\n", "requirements.txt": "flask==3.0.3\n",
         ".env": "SECRET=1\n", "node_modules/x/index.js": "x"}
VITE = {"package.json": json.dumps({"scripts": {"build": "vite build"}, "dependencies": {"react": "18"},
                                    "devDependencies": {"vite": "5"}}),
        "index.html": "<div id=root></div><script type=module src=/src/main.jsx></script>",
        "src/main.jsx": "console.log(1)"}


class UploadBuildCase(CoolifyCase):
    """Helpers for uploaded-application tests (no tests of its own)."""
    auto_link = False

    def setUp(self):
        super().setUp()
        self.configure_coolify()
        self.as_student()
        self.app.config.update(BUILD_SOURCE_URL=SOURCE_URL, BUILD_SOURCE_PEERS="")
        self.fetched = []               # what the "build server" received at start
        self.fetch_headers = {}

    # ---- helpers
    def upload(self, files, path="/api/projects/upload", name="Upload App"):
        form = {"file": (make_zip(files), "project.zip")}
        if path == "/api/projects/upload":
            form["name"] = name
        return self.client.post(path, data=form, content_type="multipart/form-data",
                                headers={"X-CSRF-Token": self.csrf()})

    def fetch(self, url, **headers):
        r = self.client.get(url.split("172.23.0.11:8000", 1)[1], headers=headers,
                            environ_base={"REMOTE_ADDR": "172.23.0.1"})
        r.get_data()
        r.close()                 # release the file (Windows cannot delete an open one)
        return r

    def fake(self):
        fake = FakeCoolify()
        route = fake._route

        def on_start(method, path, body):       # the build server reads the context when it builds
            if path.endswith(("/start", "/restart")):
                url = compose_sent(fake, next(c for c in reversed(fake.calls)
                                              if c["method"] in ("POST", "PATCH") and c["json"]
                                              and "docker_compose_raw" in c["json"])["json"])["services"]["app"]["build"]["context"]
                r = self.fetch(url, **self.fetch_headers)
                self.fetched.append((url, r.status_code, r.data if r.status_code == 200 else b""))
            return route(method, path, body)
        fake._route = on_start
        return fake

    def deploy_upload(self, files, fake=None):
        fake = fake or self.fake()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.upload(files)
            self.assertEqual(r.status_code, 201, r.get_json())
            return fake, self.wait(r.get_json()["deployment_id"]), r.get_json()["project"]["slug"]

    def project(self, slug):
        return self.app_conn().execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()

    def source_files(self, project_id):
        import pathlib
        return sorted(p.name for p in (pathlib.Path(self.app.config["INSTANCE_DIR"]) / "uploads"
                                       / str(project_id)).glob("*"))


class UploadBuildTests(UploadBuildCase):
    # ---- the happy paths
    def test_a_python_zip_is_built_by_the_engine_from_a_private_build_context(self):
        fake, d, slug = self.deploy_upload(FLASK)
        self.assertEqual(d["status"], "ready", d.get("error"))
        self.assertEqual(d["provider"], "coolify")
        self.assertTrue(d["is_upload"])
        self.assertEqual(d["url"], f"https://{slug}.apps.example.com")
        p = self.project(slug)
        self.assertEqual((p["framework"], p["source_type"], p["provider"]), ("flask", "upload", "coolify"))
        app = compose_sent(fake)["services"]["app"]
        self.assertRegex(app["build"]["context"], r"^http://172\.23\.0\.11:8000/internal/build-source/\d+/"
                                                  r"[A-Za-z0-9_-]{43}\.tar\.gz$")
        self.assertIn("gunicorn app:app", app["build"]["dockerfile_inline"])
        self.assertEqual(app["cap_drop"], ["ALL"])
        # The build server got the project, without .env or node_modules.
        url, status, data = self.fetched[0]
        self.assertEqual(status, 200)
        names = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz").getnames()
        self.assertEqual(sorted(names), ["app.py", "requirements.txt"])
        logs = self.logs(d["id"])
        self.assertIn("Detected a Python (Flask) project.", logs)
        self.assertIn("The build server received the uploaded source.", logs)
        self.assertTrue(any("environment file" in line for line in logs))
        # Once the deployment ended, the source is gone and the link is dead.
        self.assertEqual(self.source_files(p["id"]), [])
        self.assertEqual(self.fetch(url).status_code, 404)
        # The student never sees the source address.
        shown = self.client.get(f"/api/deployments/{d['id']}").get_data(as_text=True)
        self.assertNotIn("build-source", shown)

    def test_a_react_source_zip_is_built_and_a_prebuilt_one_is_still_served_directly(self):
        fake, d, slug = self.deploy_upload(VITE)
        self.assertEqual(d["status"], "ready", d.get("error"))
        self.assertEqual(self.project(slug)["framework"], "react")
        inline = compose_sent(fake)["services"]["app"]["build"]["dockerfile_inline"]
        self.assertIn("npm run build", inline)
        self.assertIn("nginx-unprivileged", inline)
        self.upgrade_student()
        with mock.patch("kova.engine.requests.request", side_effect=FakeCoolify()):
            r = self.upload({**VITE, "dist/index.html": "<h1>built</h1>"}, name="Prebuilt")
            d2 = self.wait(r.get_json()["deployment_id"])
        self.assertEqual((d2["status"], d2["provider"]), ("ready", "static"))

    def test_a_plain_static_zip_keeps_the_existing_static_hosting(self):
        fake = FakeCoolify()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.upload({"index.html": "<h1>hi</h1>"})
            d = self.wait(r.get_json()["deployment_id"])
        self.assertEqual((d["status"], d["provider"]), ("ready", "static"))
        self.assertEqual(fake.calls, [])

    def test_without_a_build_source_address_uploads_stay_static_only(self):
        self.app.config["BUILD_SOURCE_URL"] = ""
        fake = FakeCoolify()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.upload(FLASK)
            d = self.wait(r.get_json()["deployment_id"])
        self.assertEqual((d["status"], d["provider"]), ("failed", "static"))   # no index.html, as before
        self.assertEqual(fake.calls, [])

    # ---- checks before anything is built
    def test_a_python_zip_without_its_entry_point_fails_before_the_engine_is_asked(self):
        fake, d, _ = self.deploy_upload({"main2.py": "x", "requirements.txt": "flask\n"})
        self.assertEqual(d["status"], "failed")
        self.assertIn("The uploaded project does not match the Python (Flask) preset", d["error"])
        self.assertNotIn(("POST", "/services"), paths(fake))

    def test_an_uploaded_dockerfile_with_host_networking_is_refused(self):
        fake, d, _ = self.deploy_upload({"Dockerfile": "FROM python:3.12\nRUN --network=host echo hi\n"})
        self.assertEqual(d["status"], "failed")
        self.assertIn("--network", d["error"])
        self.assertNotIn(("POST", "/services"), paths(fake))

    def test_an_uploaded_dockerfile_is_used_as_is(self):
        fake, d, slug = self.deploy_upload({"Dockerfile": "FROM python:3.12-slim\nCMD [\"python\",\"-m\",\"http.server\"]\n"})
        self.assertEqual(d["status"], "ready", d.get("error"))
        build = compose_sent(fake)["services"]["app"]["build"]
        self.assertEqual(build.get("dockerfile"), "Dockerfile")
        self.assertNotIn("dockerfile_inline", build)

    def test_a_key_in_the_source_is_reported_in_words_a_student_can_read(self):
        fake, d, _ = self.deploy_upload({**FLASK, "config.py": "KEY = 'AKIA" + "A" * 16 + "'\n"})
        shown = self.client.get(f"/api/deployments/{d['id']}").get_data(as_text=True)
        self.assertIn("config.py looks like it contains an AWS access key id", shown)

    def test_the_root_directory_setting_applies_inside_the_upload(self):
        files = {"backend/app.py": FLASK["app.py"], "backend/requirements.txt": "flask\n", "README.md": "x"}
        fake, d, slug = self.deploy_upload(files)
        # Nothing runnable at the top, a Flask app in backend/: built from there, and the setting says so.
        self.assertEqual((d["status"], d["provider"]), ("ready", "coolify"), d.get("error"))
        self.assertEqual((self.project(slug)["root_dir"], self.project(slug)["framework"]), ("backend", "flask"))
        self.assertIn("Detected a Python (Flask) project in backend/", " ".join(self.logs(d["id"])))
        names = tarfile.open(fileobj=io.BytesIO(self.fetched[0][2]), mode="r:gz").getnames()
        self.assertEqual(sorted(names), ["app.py", "requirements.txt"])

    # ---- who may read the source
    def test_the_build_source_is_refused_to_anyone_but_the_build_server_during_the_build(self):
        url = {}

        def probe(url_):                  # runs while the deployment is building
            url["u"] = url_
            path = url_.split("172.23.0.11:8000", 1)[1]
            bad = re.sub(r"/[A-Za-z0-9_-]{43}\.tar\.gz$", "/" + "A" * 43 + ".tar.gz", path)
            return {
                "wrong token": self.client.get(bad, environ_base={"REMOTE_ADDR": "172.23.0.1"}).status_code,
                "through the proxy": self.client.get(path, headers={"X-Forwarded-For": "1.2.3.4"},
                                                     environ_base={"REMOTE_ADDR": "172.23.0.1"}).status_code,
                "other caller": self.client.get(path, environ_base={"REMOTE_ADDR": "172.25.0.9"}).status_code,
                "build server": self.client.get(path, environ_base={"REMOTE_ADDR": "172.23.0.1"}).status_code,
            }
        self.app.config["BUILD_SOURCE_PEERS"] = "172.23.0.1/32"
        fake = FakeCoolify()
        route, seen = fake._route, {}

        def on_start(method, path, body):
            if path.endswith("/start"):
                ctx = compose_sent(fake)["services"]["app"]["build"]["context"]
                seen.update(probe(ctx))
            return route(method, path, body)
        fake._route = on_start
        _, d, _ = self.deploy_upload(FLASK, fake)
        self.assertEqual(d["status"], "ready")
        self.assertEqual(seen, {"wrong token": 404, "through the proxy": 404, "other caller": 404,
                                "build server": 200})

    def test_the_build_source_stops_answering_after_the_fetch_limit(self):
        from kova import uploads
        self.app.config["BUILD_SOURCE_PEERS"] = ""
        fake = FakeCoolify()
        route, codes = fake._route, []

        def on_start(method, path, body):
            if path.endswith("/start"):
                ctx = compose_sent(fake)["services"]["app"]["build"]["context"]
                codes.extend(self.fetch(ctx).status_code for _ in range(uploads.SOURCE_FETCH_LIMIT + 1))
            return route(method, path, body)
        fake._route = on_start
        self.deploy_upload(FLASK, fake)
        self.assertEqual(codes, [200] * uploads.SOURCE_FETCH_LIMIT + [404])

    def test_the_source_address_is_never_served_through_the_public_site(self):
        self.assertEqual(self.client.get("/internal/build-source/1/" + "A" * 43 + ".tar.gz",
                                         headers={"X-Forwarded-For": "1.2.3.4"}).status_code, 404)

    # ---- redeploys and switching kinds
    def test_a_new_upload_of_a_different_kind_resets_the_untouched_build_settings(self):
        fake, d, slug = self.deploy_upload(FLASK)
        self.assertEqual(self.project(slug)["framework"], "flask")
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.upload({"requirements.txt": "fastapi\nuvicorn\n", "main.py": "app = 1\n"},
                            path=f"/api/projects/{slug}/upload")
            d2 = self.wait(r.get_json()["deployment"]["id"])
        self.assertEqual(d2["status"], "ready", d2.get("error"))
        self.assertEqual(self.project(slug)["framework"], "fastapi")
        self.assertEqual(d2["expires_at"], d["expires_at"])          # free plan keeps its one window

    def test_customised_settings_survive_a_new_upload_of_the_same_kind(self):
        fake, d, slug = self.deploy_upload(FLASK)
        self.call("patch", f"/api/projects/{slug}", {"framework": "fastapi",
                                                     "start_command": "uvicorn app:app --host 0.0.0.0 --port 8000"})
        fake.calls.clear()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.upload(FLASK, path=f"/api/projects/{slug}/upload")
            self.wait(r.get_json()["deployment"]["id"])
        self.assertEqual(self.project(slug)["framework"], "fastapi")
        self.assertIn("uvicorn app:app", compose_sent(fake, next(
            c for c in fake.calls if c["method"] == "PATCH" and c["json"] and "docker_compose_raw" in c["json"])["json"])
            ["services"]["app"]["build"]["dockerfile_inline"])

    def test_a_static_upload_after_an_app_upload_removes_the_app(self):
        fake, d, slug = self.deploy_upload(FLASK)
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.upload({"index.html": "<h1>hi</h1>"}, path=f"/api/projects/{slug}/upload")
            d2 = self.wait(r.get_json()["deployment"]["id"])
        self.assertEqual((d2["status"], d2["provider"]), ("ready", "static"))
        self.assertIn(("DELETE", "/services/svc-123"), paths(fake))
        self.assertFalse(self.project(slug)["provider_ref"])

    def test_expiry_removes_the_uploaded_app(self):
        from kova import engine
        fake, d, slug = self.deploy_upload(FLASK)
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            engine.expire_deployment(self.app_conn(), self.app.config, {"deployment_id": d["id"]}, lambda: None)
        self.assertIn(("DELETE", "/services/svc-123"), paths(fake))
        row = self.app_conn().execute("SELECT status FROM deployments WHERE id = ?", (d["id"],)).fetchone()
        self.assertEqual(row["status"], "expired")


class UploadContextPolicyTests(CoolifyCase):
    def test_only_the_build_source_route_is_accepted_as_a_non_github_context(self):
        good = SOURCE_URL + "/internal/build-source/12/" + "a" * 43 + ".tar.gz"
        doc = composegen.generate_service(slug="demo", deployment=12, app_context=good)
        self.assertEqual(doc["services"]["app"]["build"]["context"], good)
        for bad in ("http://evil.example/x.tar.gz", SOURCE_URL + "/internal/build-source/12/short.tar.gz",
                    SOURCE_URL + "/live/x/" + "a" * 43 + ".tar.gz", "file:///etc/passwd"):
            with self.assertRaises(composegen.ComposeError, msg=bad):
                composegen.generate_service(slug="demo", deployment=12, app_context=bad)
        tampered = json.loads(json.dumps(doc))
        tampered["services"]["app"]["build"]["context"] = "http://evil.example/x.tar.gz"
        with self.assertRaises(composegen.ComposeError):
            composegen.assert_safe_service(tampered)
