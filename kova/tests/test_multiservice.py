"""Multi-service (docker-compose) deployments, new presets and honest handling of non-web projects."""
import copy
import json
import unittest
from unittest import mock

from kova import composegen, coolifygw, multiservice, projectscan
from kova.buildpolicy import dockerfile_problems
from kova.multiservice import ComposePlanError, parse, plan
from .test_coolify import SAFE_DOCKERFILE, CoolifyCase, FakeCoolify, compose_sent, paths
from .test_upload_builds import SOURCE_URL, UploadBuildCase
from .test_uploads import make_zip

STACK = """
services:
  web:
    build: ./web
    ports: ["3000:3000"]
    environment:
      API_URL: http://api:8000
    depends_on: [api]
  api:
    build: { context: ./api }
    environment:
      - DATABASE_URL=postgresql://app:secret@db:5432/app
      - REDIS_URL=redis://cache:6379/0
      - WEB_URL=http://web:3000
    volumes: ["./api:/code"]
    depends_on: { db: { condition: service_healthy } }
  worker:
    build: ./api
    command: python worker.py --queue $QUEUE
    depends_on: [cache]
  db:
    image: postgres:16-alpine
    environment: { POSTGRES_USER: app, POSTGRES_PASSWORD: secret, POSTGRES_DB: app }
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U app"], interval: 5s, retries: 10 }
    ports: ["5432:5432"]
  cache:
    image: redis:7-alpine
volumes:
  pgdata:
"""
FILES = {"web/Dockerfile", "api/Dockerfile", "docker-compose.yml"}
CTX = "https://github.com/acme/web-app.git#" + "a" * 40


def stack_plan(text=STACK, files=FILES):
    return plan(parse(text), files)


def generated(p=None):
    p = p or stack_plan()
    return composegen.generate_multi(slug="web-app", deployment=4, plan=p,
                                     contexts={s["name"]: f"{CTX}:{s['context']}" for s in p["services"]
                                               if s["kind"] == "build"}, env_keys=["OPENAI_API_KEY"])


class PlanTests(unittest.TestCase):
    def test_a_full_stack_compose_file_is_translated(self):
        p = stack_plan()
        self.assertEqual([s["name"] for s in p["services"]], ["app", "api", "worker", "db", "cache"])
        self.assertEqual(p["port"], 3000)
        api = next(s for s in p["services"] if s["name"] == "api")
        self.assertEqual(api["environment"]["WEB_URL"], "http://app:3000")         # renamed reference
        self.assertEqual(api["depends_on"], {"db": "service_healthy"})         # the condition is kept
        self.assertEqual(generated()["services"]["app"]["depends_on"], {"api": {"condition": "service_started"}})
        self.assertTrue(any("./api -> /code" in w for w in p["warnings"]))
        worker = next(s for s in p["services"] if s["name"] == "worker")
        self.assertEqual(worker["command"], "python worker.py --queue ${QUEUE}")

    def test_the_generated_file_has_the_platform_hardening_everywhere(self):
        doc = generated()
        self.assertEqual(set(doc["services"]), {"app", "api", "worker", "db", "cache"})
        for name, svc in doc["services"].items():
            with self.subTest(name=name):
                self.assertEqual(svc["cap_drop"], ["ALL"])
                self.assertEqual(svc["security_opt"], ["no-new-privileges:true"])
                self.assertNotIn("ports", svc)
                self.assertNotIn("networks", svc)
        self.assertEqual(doc["services"]["app"]["expose"], ["3000"])
        self.assertNotIn("cap_add", doc["services"]["api"])
        self.assertEqual(doc["services"]["db"]["cap_add"], multiservice.DATA_CAPS)
        self.assertEqual(doc["services"]["db"]["volumes"],
                         [{"type": "volume", "source": "pgdata", "target": "/var/lib/postgresql/data"}])
        self.assertEqual(doc["volumes"], {"pgdata": {}})
        self.assertEqual(doc["services"]["api"]["environment"]["OPENAI_API_KEY"], "${OPENAI_API_KEY}")
        self.assertEqual(doc["services"]["db"]["mem_limit"], "256M")

    def test_host_access_and_vulnerable_labs_are_refused(self):
        cases = {
            "privileged": "services:\n  app:\n    build: .\n    ports: ['80']\n    privileged: true\n",
            "host network": "services:\n  app:\n    build: .\n    ports: ['80']\n    network_mode: host\n",
            "docker socket": "services:\n  app:\n    build: .\n    ports: ['80']\n    volumes: ['/var/run/docker.sock:/var/run/docker.sock']\n",
            "cap_add": "services:\n  app:\n    build: .\n    ports: ['80']\n    cap_add: [NET_ADMIN]\n",
            "extra_hosts": "services:\n  app:\n    build: .\n    ports: ['80']\n    extra_hosts: ['h:host-gateway']\n",
            "juice shop": "services:\n  app:\n    build: .\n    ports: ['80']\n  lab:\n    image: bkimminich/juice-shop\n",
            "dvwa": "services:\n  app:\n    build: .\n    ports: ['80']\n  lab:\n    image: vulnerables/web-dvwa\n",
            "other image": "services:\n  app:\n    build: .\n    ports: ['80']\n  proxy:\n    image: nginx:latest\n",
            "secrets": "services:\n  app:\n    build: .\n    ports: ['80']\nsecrets:\n  k:\n    file: ./k.txt\n",
            "no public port": "services:\n  worker:\n    build: .\n  db:\n    image: postgres:16\n",
            "too many": "services:\n" + "".join(f"  s{i}:\n    build: .\n    ports: ['80']\n" for i in range(7)),
            "too much memory": "services:\n  app:\n    build: .\n    ports: ['80']\n" + "".join(
                f"  m{i}:\n    image: mysql:8\n" for i in range(3)),
        }
        for what, text in cases.items():
            with self.subTest(what=what), self.assertRaises(ComposePlanError):
                plan(parse(text), {"Dockerfile"})

    def test_a_vulnerable_base_image_in_a_dockerfile_is_refused(self):
        self.assertTrue(dockerfile_problems("FROM vulnerables/web-dvwa\n"))
        self.assertTrue(dockerfile_problems("FROM webgoat/webgoat:latest\n"))
        self.assertEqual(dockerfile_problems("FROM python:3.12-slim\n"), [])

    def test_tampering_with_a_generated_file_is_refused(self):
        def tamper(fn):
            doc = copy.deepcopy(generated())
            fn(doc)
            with self.assertRaises(composegen.ComposeError):
                composegen.assert_safe_service(doc)
        tamper(lambda d: d["services"]["api"].update(privileged=True))
        tamper(lambda d: d["services"]["api"].update(cap_add=["NET_ADMIN"]))
        tamper(lambda d: d["services"]["db"].update(image="nginx:latest"))
        tamper(lambda d: d["services"]["db"].update(cap_add=["SYS_ADMIN"]))
        tamper(lambda d: d["services"]["api"].update(volumes=[{"type": "bind", "source": "/", "target": "/host"}]))
        tamper(lambda d: d["services"]["api"].update(expose=["22"]))
        tamper(lambda d: d["services"]["api"]["environment"].update(X="$HOME"))
        tamper(lambda d: d["services"]["api"]["environment"].update(SERVICE_FQDN_APP="x"))
        tamper(lambda d: d["services"]["api"]["build"].update(context="http://evil.example/x.tar.gz"))
        tamper(lambda d: d["services"]["api"].update(cap_drop=[]))
        tamper(lambda d: d.update(networks={"host": {"external": True}}))
        tamper(lambda d: d["services"].pop("app"))

    def test_the_gateway_checks_every_build_context(self):
        cfg = {"project_uuid": "p", "server_uuid": "s", "destination_uuid": "d", "environment_name": "production",
               "base_domain": "apps.codeskys.com", "build_source_url": "http://172.23.0.11:8000"}
        import base64
        doc = generated()
        body = {"project_uuid": "p", "server_uuid": "s", "destination_uuid": "d", "environment_name": "production",
                "name": "web-app", "instant_deploy": False, "connect_to_docker_network": False,
                "description": "Managed by Codeskys - project 3 (web-app). Do not edit.",
                "docker_compose_raw": base64.b64encode(composegen.render(doc).encode()).decode(),
                "urls": [{"name": "app", "url": "http://web-app.apps.codeskys.com:3000"}]}
        coolifygw.check("POST", "/services", {}, body, cfg)
        doc["services"]["api"]["build"]["context"] = "http://172.23.0.2:8080/internal/build-source/4/" + "a" * 43 + ".tar.gz"
        body["docker_compose_raw"] = base64.b64encode(composegen.render(doc).encode()).decode()
        with self.assertRaises(coolifygw.Refused):
            coolifygw.check("POST", "/services", {}, body, cfg)


class ScanTests(unittest.TestCase):
    def read(self, data):
        return lambda p: data.get(p)

    def test_choose(self):
        cases = [
            ({"docker-compose.yml": STACK, "web/Dockerfile": "", "api/Dockerfile": ""}, "compose"),
            ({"requirements.txt": "gradio\n", "app.py": "import gradio"}, "gradio"),
            ({"requirements.txt": "dash\nflask\nplotly\npandas\n", "app.py": "import dash"}, "dash"),
            ({"package.json": json.dumps({"dependencies": {"@nestjs/core": "10"}})}, "nestjs"),
            ({"analysis.ipynb": "{}", "requirements.txt": "pandas\nscikit-learn\n"}, "docs"),
            ({"README.md": "# report", "main.tf": "x"}, "docs"),
            ({"main.tf": "x", "vars.tf": "y"}, None),
            ({"etl.py": "print(1)", "requirements.txt": "pandas\n"}, None),
            ({"index.html": "<h1>"}, "static"),
            ({"app.py": "from flask import Flask", "requirements.txt": "flask\n"}, "flask"),
        ]
        for data, want in cases:
            with self.subTest(files=sorted(data)):
                self.assertEqual(projectscan.choose(set(data), self.read(data)), want)

    def test_classify(self):
        self.assertEqual(projectscan.classify(set(), self.read({}))["category"], "empty")
        data = {"docker-compose.yml": STACK, "web/Dockerfile": "FROM node:20", "api/Dockerfile": "FROM python:3.12"}
        self.assertEqual(projectscan.classify(set(data), self.read(data))["category"], "multi")
        data = {"docker-compose.yml": "services:\n  app:\n    build: .\n    ports: ['3000']\n  lab:\n    image: bkimminich/juice-shop\n",
                "Dockerfile": "FROM node:20"}
        self.assertEqual(projectscan.classify(set(data), self.read(data))["category"], "unsafe")
        data = {"backend/app.py": "from flask import Flask", "backend/requirements.txt": "flask"}
        c = projectscan.classify(set(data), self.read(data))          # Codeskys sets the Root directory itself
        self.assertEqual((c["category"], c["framework"], c["root_dir"]), ("ready", "flask", "backend"))
        data = {"main.py": "x", "requirements.txt": "flask\n"}
        self.assertEqual(projectscan.classify(set(data), self.read(data))["category"], "configure")
        data = {"app.py": "from flask import Flask", "requirements.txt": "flask\n"}
        self.assertEqual(projectscan.classify(set(data), self.read(data))["category"], "ready")
        data = {"notes.ipynb": "{}"}
        self.assertEqual(projectscan.classify(set(data), self.read(data))["category"], "non_web")

    def test_the_real_entry_file_is_found(self):
        data = {"streamlit_app.py": "import streamlit as st\n", "requirements.txt": "streamlit\n"}
        self.assertIn("streamlit run streamlit_app.py", projectscan.entry_start("streamlit", set(data), self.read(data)))
        self.assertEqual(projectscan.classify(set(data), self.read(data))["category"], "ready")
        data = {"app.py": "from fastapi import FastAPI\napi = FastAPI()\n"}
        self.assertEqual(projectscan.entry_start("fastapi", set(data), self.read(data)),
                         "uvicorn app:api --host 0.0.0.0 --port 8000")
        data = {"app.py": "import streamlit as st\n"}            # the preset already fits: nothing changes
        self.assertIsNone(projectscan.entry_start("streamlit", set(data), self.read(data)))

    def test_detection_works_when_the_github_api_limit_is_used_up(self):
        from kova import gitinfo
        refs = (b"001e# service=git-upload-pack\n0000"
                + b"0050" + b"a" * 40 + b" HEAD\0symref=HEAD:refs/heads/main\n" + b"003f" + b"a" * 40 + b" refs/heads/main\n0000")
        raw = {"requirements.txt": "streamlit\n", "streamlit_app.py": "import streamlit as st\n"}

        class Resp:
            def __init__(self, status, text=""):
                self.status_code, self.text, self.content = status, text, text.encode() if isinstance(text, str) else text

        def get(url, **kw):
            if url.endswith("/info/refs"):
                return Resp(200, refs)
            name = url.rsplit("/", 1)[1]
            return Resp(200, raw[name]) if name in raw else Resp(404)
        with mock.patch("kova.gitinfo._gh", return_value=Resp(403)), mock.patch("kova.gitinfo.requests.get", side_effect=get):
            r = gitinfo.inspect_repo("https://github.com/acme/rides")
        self.assertEqual((r["framework"], r["default_branch"], r["reachable"]), ("streamlit", "main", True))
        self.assertTrue(r["detected_without_api"])

    def test_the_file_list_comes_from_the_archive_when_the_api_limit_is_used_up(self):
        import io
        import tarfile
        from kova import buildpolicy
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name in ("rides-abc/streamlit_app.py", "rides-abc/data/x.csv"):
                info = tarfile.TarInfo(name)
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))

        class Resp:
            def __init__(self, status, raw=b""):
                self.status_code, self.raw = status, io.BytesIO(raw)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        calls = []

        def get(url, **kw):
            calls.append(url)
            return Resp(403) if "api.github.com" in url else Resp(200, buf.getvalue())
        with mock.patch("kova.buildpolicy.requests.get", side_effect=get):
            files = buildpolicy.repository_files("https://github.com/acme/rides", "a" * 40)
        self.assertEqual(files, {"streamlit_app.py", "data/x.csv"})
        self.assertTrue(calls[1].startswith("https://codeload.github.com/acme/rides/tar.gz/"))

    def test_paths_on_the_students_computer_are_found(self):
        data = {"app.py": 'model = YOLO(r"C:\\Users\\krish\\OneDrive\\Desktop\\pothole_app\\best.pt")\n',
                "util.py": "open('/home/asha/data.csv')", "ok.py": "open('data/x.csv')", "sub/x.py": '"C:\\Users\\a\\b"'}
        found = dict(projectscan.machine_paths(set(data), self.read(data)))
        self.assertEqual(set(found), {"app.py", "util.py"})
        self.assertIn("pothole_app", found["app.py"])

    def test_the_refusal_says_what_the_project_contains(self):
        msg = projectscan.refusal({"main.tf", "vars.tf", ".github/workflows/ci.yml"})
        self.assertIn("no web application", msg)
        self.assertIn("2 Terraform files", msg)


class ComposeDeploymentTests(CoolifyCase):
    """A GitHub repository with a docker-compose file, deployed end to end against the fake."""

    def setUp(self):
        super().setUp()
        self.configure_coolify()
        self.as_student()
        self.repo = {"docker-compose.yml": STACK, "web/Dockerfile": SAFE_DOCKERFILE, "api/Dockerfile": SAFE_DOCKERFILE}
        for target, fn in (("kova.buildpolicy.fetch_file", lambda repo, sha, path, **k: self.repo.get(path)),
                           ("kova.buildpolicy.repository_files", lambda *a, **k: set(self.repo))):
            patcher = mock.patch(target, side_effect=fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_compose_repository_deploys_as_one_isolated_multi_service_app(self):
        fake = FakeCoolify()
        d = self.deploy_with(fake, framework="compose")
        self.assertEqual(d["status"], "ready", d)
        doc = compose_sent(fake)
        self.assertEqual(set(doc["services"]), {"app", "api", "worker", "db", "cache"})
        self.assertTrue(doc["services"]["app"]["build"]["context"].endswith(":web"))
        self.assertTrue(doc["services"]["worker"]["build"]["context"].endswith(":api"))
        logs = " ".join(self.logs(d["id"]))
        self.assertIn("Docker Compose project with 5 services", logs)
        self.assertIn("db (postgres:16-alpine)", logs)

    def test_a_compose_repository_with_a_vulnerable_lab_is_refused_before_the_engine_is_asked(self):
        self.repo["docker-compose.yml"] = STACK.replace("volumes:\n  pgdata:",
                                                        "  lab:\n    image: bkimminich/juice-shop\nvolumes:\n  pgdata:")
        fake = FakeCoolify()
        d = self.deploy_with(fake, framework="compose")
        self.assertEqual(d["status"], "failed")
        self.assertIn("intentionally vulnerable", d["error"])
        self.assertNotIn(("POST", "/services"), paths(fake))

    def test_a_finished_one_off_service_does_not_block_going_live(self):
        # A migrate/seed container that exits makes Coolify report the whole service "degraded".
        fake = FakeCoolify(app_statuses=["starting:unhealthy", "degraded:unhealthy"])
        d = self.deploy_with(fake, framework="compose")
        self.assertEqual(d["status"], "ready", d)

    def test_unknown_depends_on_conditions_are_refused(self):
        self.repo["docker-compose.yml"] = STACK.replace("condition: service_healthy", "condition: whenever")
        d = self.deploy_with(FakeCoolify(), framework="compose")
        self.assertEqual(d["status"], "failed")
        self.assertIn("depends_on condition", d["error"])

    def _devops_demo(self):
        # Production 2026-11-14 (deployment 38): framework "Dockerfile", only a README at the top,
        # the real app is a compose stack in forgeops/ whose database password comes from the project.
        compose = STACK.replace("POSTGRES_PASSWORD: secret", 'POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"')
        self.repo = {"README.md": "# demo", "forgeops/docker-compose.yml": compose,
                     "forgeops/web/Dockerfile": SAFE_DOCKERFILE, "forgeops/api/Dockerfile": SAFE_DOCKERFILE}
        self.app.config["GIT_LOOKUPS"] = True

    def test_a_dockerfile_project_without_a_dockerfile_is_built_as_what_it_really_is(self):
        self._devops_demo()
        fake = FakeCoolify()
        with mock.patch("kova.engine.resolve_commit", return_value=("f" * 40, "demo")):
            d = self.deploy_with(fake, framework="docker",
                                 env=[{"key": "POSTGRES_PASSWORD", "value": "chosen-by-the-student"}])
        self.assertEqual(d["status"], "ready", d.get("error"))
        project = self.app_conn().execute("SELECT framework, root_dir FROM projects").fetchone()
        self.assertEqual((project["framework"], project["root_dir"]), ("compose", "forgeops"))
        self.assertIn("there is no Dockerfile in the top level", " ".join(self.logs(d["id"])))
        self.assertTrue(compose_sent(fake)["services"]["app"]["build"]["context"].endswith(":forgeops/web"))

    def _devops_demo_all_variables(self):
        # Like DeVops-Demo-Project: user, password and database all come from the project.
        self._devops_demo()
        self.repo["forgeops/docker-compose.yml"] = STACK.replace(
            "environment: { POSTGRES_USER: app, POSTGRES_PASSWORD: secret, POSTGRES_DB: app }",
            'environment: { POSTGRES_USER: "${POSTGRES_USER}", POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}", '
            'POSTGRES_DB: "${POSTGRES_DB}" }').replace(
            "DATABASE_URL=postgresql://app:secret@db:5432/app",
            "DATABASE_URL=postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}")

    def _sent(self, fake):
        return {k: v["value"] for k, v in next(iter(fake.services.values()))["envs"].items()}

    def test_codeskys_prepares_the_database_settings_itself(self):
        self._devops_demo_all_variables()
        fake = FakeCoolify()
        with mock.patch("kova.engine.resolve_commit", return_value=("f" * 40, "demo")):
            d = self.deploy_with(fake, framework="docker")
        self.assertEqual(d["status"], "ready", d.get("error"))
        sent = self._sent(fake)
        self.assertRegex(sent["POSTGRES_PASSWORD"], r"^[A-Za-z0-9]{32}$")
        self.assertRegex(sent["POSTGRES_USER"], r"^u_[a-z0-9]{10}$")
        self.assertRegex(sent["POSTGRES_DB"], r"^db_[a-z0-9]{10}$")
        # The compose file refers to them; the values exist only in the engine's variables.
        doc = compose_sent(fake)
        self.assertEqual(doc["services"]["db"]["environment"]["POSTGRES_PASSWORD"], "${POSTGRES_PASSWORD}")
        self.assertIn("${POSTGRES_PASSWORD}@db:5432", doc["services"]["api"]["environment"]["DATABASE_URL"])
        slug = self.app_conn().execute("SELECT slug FROM projects").fetchone()["slug"]
        shown = json.dumps([self.logs(d["id"]), d,
                                        self.client.get(f"/api/projects/{slug}/env").get_json(),
                                        self.client.get(f"/api/projects/{slug}").get_json()])
        for value in sent.values():
            if value in (sent["POSTGRES_PASSWORD"], sent["POSTGRES_USER"], sent["POSTGRES_DB"]):
                self.assertNotIn(value, shown)
        self.assertIn("prepared private database settings", shown)
        empty = [l for l in self.logs(d["id"]) if "so they are empty" in l]
        self.assertFalse(any("POSTGRES" in l for l in empty), empty)          # generated values are not "unset"

        # A redeploy keeps them (the database volume still has the old password)...
        rows = self.app_conn().execute("SELECT key, value_cipher FROM managed_credentials ORDER BY key").fetchall()
        with mock.patch("kova.engine.requests.request", side_effect=fake),                 mock.patch("kova.engine.resolve_commit", return_value=("e" * 40, "demo 2")):
            r = self.client.post(f"/api/projects/{slug}/deploy", json={}, headers={"X-CSRF-Token": self.csrf()})
            self.assertEqual(self.wait(r.get_json()["deployment"]["id"])["status"], "ready")
        self.assertEqual(self._sent(fake)["POSTGRES_PASSWORD"], sent["POSTGRES_PASSWORD"])
        self.assertEqual(rows, self.app_conn().execute("SELECT key, value_cipher FROM managed_credentials ORDER BY key").fetchall())
        # ...and the teardown at expiry removes them with the database.
        from kova import engine
        conn = self.app_conn()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            engine.teardown_coolify_app(conn, self.app.config, conn.execute("SELECT * FROM projects").fetchone())
        self.assertEqual(self.app_conn().execute("SELECT COUNT(*) AS n FROM managed_credentials").fetchone()["n"], 0)

    def test_a_students_own_database_setting_is_never_replaced(self):
        self._devops_demo_all_variables()
        fake = FakeCoolify()
        with mock.patch("kova.engine.resolve_commit", return_value=("f" * 40, "demo")):
            d = self.deploy_with(fake, framework="docker",
                                 env=[{"key": "POSTGRES_PASSWORD", "value": "chosen-by-the-student"}])
        self.assertEqual(d["status"], "ready", d.get("error"))
        sent = self._sent(fake)
        self.assertEqual(sent["POSTGRES_PASSWORD"], "chosen-by-the-student")
        self.assertRegex(sent["POSTGRES_USER"], r"^u_")
        keys = [r["key"] for r in self.app_conn().execute("SELECT key FROM managed_credentials")]
        self.assertNotIn("POSTGRES_PASSWORD", keys)

    def test_only_settings_of_databases_codeskys_runs_are_generated(self):
        p = plan(parse(STACK.replace("POSTGRES_PASSWORD: secret", 'POSTGRES_PASSWORD: "${DB_PASS}"')
                       .replace("REDIS_URL=redis://cache:6379/0", "OPENAI_API_KEY=${OPENAI_API_KEY}")), FILES)
        self.assertEqual(p["db_credentials"], {"DB_PASS": "password"})          # named by the database's setting
        self.assertIn("OPENAI_API_KEY", p["required_env"])                     # a third-party key: never generated

    def test_a_dockerfile_project_with_a_dockerfile_is_unchanged(self):
        self.repo = {"Dockerfile": SAFE_DOCKERFILE, "docker-compose.yml": STACK, "README.md": "x"}
        self.app.config["GIT_LOOKUPS"] = True
        fake = FakeCoolify()
        with mock.patch("kova.engine.resolve_commit", return_value=("f" * 40, "own Dockerfile")):
            d = self.deploy_with(fake, framework="docker")
        self.assertEqual(d["status"], "ready", d.get("error"))
        self.assertEqual(self.app_conn().execute("SELECT framework FROM projects").fetchone()["framework"], "docker")
        self.assertEqual(set(compose_sent(fake)["services"]), {"app"})

    def test_a_docs_project_with_the_app_in_a_folder_is_built_as_the_app(self):
        # Production 2026-11-14: the create form proposed "Notebook & docs preview" for this repository.
        self._devops_demo()
        with mock.patch("kova.engine.resolve_commit", return_value=("f" * 40, "demo")):
            d = self.deploy_with(FakeCoolify(), framework="docs",
                                 env=[{"key": "POSTGRES_PASSWORD", "value": "chosen-by-the-student"}])
        self.assertEqual(d["status"], "ready", d.get("error"))
        project = self.app_conn().execute("SELECT framework, root_dir FROM projects").fetchone()
        self.assertEqual((project["framework"], project["root_dir"]), ("compose", "forgeops"))

    def test_the_students_forgeops_folder_is_not_hidden_from_them(self):
        from kova.security import for_student
        shown = "Codeskys detected a Docker Compose project in forgeops/ and builds it that way."
        self.assertEqual(for_student(shown), shown)
        self.assertEqual(for_student("forgeops runtime proxy refused the request"), "Deployment unavailable. Please contact support.")
        self.assertNotIn("/opt/", for_student("failed at /opt/codeskys/forgeops/ops/x.yml"))

    def test_the_create_form_proposes_the_app_in_a_folder(self):
        from kova import gitinfo
        self._devops_demo()
        result = {"default_branch": "main", "root_dir": "", "framework": "docs", "private": False}
        with mock.patch("kova.gitinfo.branch_head", return_value="a" * 40), \
                mock.patch("kova.buildpolicy.repository_files", return_value=set(self.repo)), \
                mock.patch("kova.buildpolicy.fetch_file", side_effect=lambda u, s, p, **k: self.repo.get(p)):
            gitinfo._detect_in_folder({"full_name": "acme/demo"}, result)
        self.assertEqual((result["framework"], result["root_dir"]), ("compose", "forgeops"))

    def test_a_database_without_a_password_is_refused_with_the_fix(self):
        with self.assertRaises(ComposePlanError) as ctx:
            plan(parse(STACK.replace("POSTGRES_PASSWORD: secret, ", "")), FILES)
        self.assertIn("POSTGRES_PASSWORD", str(ctx.exception))

    def test_a_static_repository_without_a_home_page_never_goes_live(self):
        # The GitHub API limit is used up (no file list): the home page is still checked, and the
        # generated image can never fall back to nginx's own welcome page.
        self.repo = {"streamlit_app.py": "import streamlit as st\n", "README.md": "x"}
        self.app.config["GIT_LOOKUPS"] = True
        fake = FakeCoolify()
        with mock.patch("kova.buildpolicy.repository_files", return_value=None),                 mock.patch("kova.engine.resolve_commit", return_value=("f" * 40, "no home page")):
            d = self.deploy_with(fake, framework="static")
        self.assertEqual(d["status"], "failed")
        self.assertIn("no index.html", d["error"])
        self.assertNotIn(("POST", "/services"), paths(fake))
        from kova import dockerfiles
        text = dockerfiles.generate({"framework": "static"})[0]
        self.assertLess(text.index("rm -rf /usr/share/nginx/html"), text.index("COPY"))
        self.assertIn("test -f /usr/share/nginx/html/index.html", text)

    def test_a_vulnerable_lab_repository_is_refused_by_name(self):
        fake = FakeCoolify()
        d = self.deploy_with(fake, framework="node", repo_url="https://github.com/juice-shop/juice-shop")
        self.assertEqual(d["status"], "failed")
        self.assertIn("The repository juice-shop/juice-shop is an intentionally vulnerable", d["error"])
        self.assertNotIn(("POST", "/services"), paths(fake))

    def test_a_service_dockerfile_asking_for_host_networking_is_refused(self):
        self.repo["api/Dockerfile"] = "FROM python:3.12\nRUN --network=host true\n"
        d = self.deploy_with(FakeCoolify(), framework="compose")
        self.assertEqual(d["status"], "failed")
        self.assertIn("The Dockerfile of api", d["error"])


class UploadComposeTests(UploadBuildCase):
    def test_a_multi_service_zip_builds_each_service_from_its_own_folder(self):
        files = {"docker-compose.yml": STACK, "web/Dockerfile": SAFE_DOCKERFILE, "web/index.js": "x",
                 "api/Dockerfile": SAFE_DOCKERFILE, "api/main.py": "x"}
        fake = FakeCoolify()
        fetched = {}
        route = fake._route

        def on_start(method, path, body):
            if path.endswith("/start"):
                doc = compose_sent(fake)
                for name in ("app", "api", "worker"):
                    url = doc["services"][name]["build"]["context"]
                    r = self.fetch(url)
                    import io
                    import tarfile
                    fetched[name] = (r.status_code, sorted(tarfile.open(fileobj=io.BytesIO(r.data), mode="r:gz").getnames())
                                     if r.status_code == 200 else [])
            return route(method, path, body)
        fake._route = on_start
        _, d, slug = self.deploy_upload(files, fake)
        self.assertEqual(d["status"], "ready", d.get("error"))
        self.assertEqual(self.project(slug)["framework"], "compose")
        self.assertEqual(fetched["app"], (200, ["Dockerfile", "index.js"]))
        self.assertEqual(fetched["api"], (200, ["Dockerfile", "main.py"]))
        self.assertEqual(fetched["worker"][1], fetched["api"][1])
        self.assertEqual(self.source_files(self.project(slug)["id"]), [])

    def test_a_notebook_zip_becomes_a_labelled_preview_not_a_fake_site(self):
        _, d, slug = self.deploy_upload({"analysis.ipynb": "{}", "README.md": "# Findings",
                                         "requirements.txt": "pandas\n"})
        self.assertEqual(d["status"], "ready", d.get("error"))
        self.assertEqual(self.project(slug)["framework"], "docs")

    def test_a_dockerfile_project_listens_on_the_port_it_exposes(self):
        dockerfile = SAFE_DOCKERFILE.rstrip("\n") + "\nEXPOSE 8080\n"
        fake = FakeCoolify()
        _, d, slug = self.deploy_upload({"Dockerfile": dockerfile, "app.py": "x"}, fake)
        self.assertEqual(d["status"], "ready", d.get("error"))
        self.assertEqual(self.project(slug)["port"], 8080)
        self.assertEqual(compose_sent(fake)["services"]["app"]["expose"], ["8080"])

    def test_an_upload_whose_app_is_not_in_app_py_still_starts(self):
        fake = FakeCoolify()
        _, d, slug = self.deploy_upload({"streamlit_app.py": "import streamlit as st\n",
                                         "requirements.txt": "streamlit\n"}, fake)
        self.assertEqual(d["status"], "ready", d.get("error"))
        self.assertIn("streamlit run streamlit_app.py", self.project(slug)["start_command"])
        self.assertIn("streamlit run streamlit_app.py", compose_sent(fake)["services"]["app"]["build"]["dockerfile_inline"])

    def test_an_uploaded_vulnerable_lab_is_refused_before_the_engine_is_asked(self):
        # Juice Shop's own Dockerfile is an ordinary node image: its package name gives it away.
        fake = FakeCoolify()
        _, d, _ = self.deploy_upload({"package.json": json.dumps({"name": "juice-shop", "scripts": {"start": "node app"}}),
                                      "app.js": "x"}, fake)
        self.assertEqual(d["status"], "failed")
        self.assertIn("intentionally vulnerable", d["error"])
        self.assertNotIn(("POST", "/services"), paths(fake))

    def test_a_terraform_zip_is_refused_with_what_it_contains(self):
        fake = FakeCoolify()
        with mock.patch("kova.engine.requests.request", side_effect=fake):
            r = self.upload({"main.tf": "x", "variables.tf": "y"})
            d = self.wait(r.get_json()["deployment_id"])
        self.assertEqual(d["status"], "failed")
        self.assertIn("no web application", d["error"])
        self.assertIn("2 Terraform files", d["error"])
        self.assertEqual(fake.calls, [])
