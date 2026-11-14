"""Entry-point preflight: a preset that cannot start in this repository fails before the build.

Live case (2026-11-14, deployment 15): a two-service Compose repository (FastAPI backend +
Streamlit frontend, no top-level app.py) was given the Flask preset, built for eight minutes
into a 10.8 GB image and then crash-looped on ``gunicorn app:app``."""
import unittest
from unittest import mock

from kova import buildpolicy, dockerfiles

from .test_coolify import CoolifyCase, FakeCoolify, paths

# The layout of akshayavarshinik7226-code/rag-company-knowledge-chatbot (read from GitHub).
RAG_FILES = {
    ".dockerignore", ".env.example", ".gitignore", "Dockerfile.backend", "Dockerfile.frontend", "README.md",
    "docker-compose.yml", "requirements.backend.txt", "requirements.txt",
    "backend/__init__.py", "backend/main.py", "backend/chat.py", "backend/rag_chain.py",
    "frontend/app.py", "tests/test_api.py", "data/handbook.md",
}


def project(framework, **extra):
    return {"framework": framework, "root_dir": "", "install_command": "", "build_command": "",
            "start_command": "", "output_dir": "", "port": 8000, "node_version": "20.x", **extra}


def problem(framework, files=RAG_FILES, **extra):
    p = project(framework, **extra)
    s = dockerfiles.settings(p) | {"label": framework}
    return buildpolicy.entrypoint_problem(p, s, files)


class RequiredFiles(unittest.TestCase):
    def test_server_commands(self):
        rf = buildpolicy.required_files
        self.assertEqual(rf("", "gunicorn app:app -b 0.0.0.0:8000"),
                         [("the module app that the start command runs", ["app.py", "app/__init__.py"])])
        self.assertEqual(rf("", "uvicorn backend.main:app --host 0.0.0.0 --port 8000")[0][1],
                         ["backend/main.py", "backend/main/__init__.py"])
        self.assertEqual(rf("", "gunicorn -b localhost:8000 -w 2 config.wsgi")[0][1],
                         ["config/wsgi.py", "config/wsgi/__init__.py"], "option values are not the module")
        self.assertEqual(rf("", "gunicorn 'app:create_app()'")[0][1][0], "app.py")
        self.assertEqual(rf("", "streamlit run frontend/app.py")[0][1], ["frontend/app.py"])
        self.assertEqual(rf("", "python3 server.py")[0][1], ["server.py"])
        self.assertEqual(rf("", "uvicorn --app-dir src main:app"), [], "a moved base directory is not checked")
        self.assertEqual(rf("", "npm start"), [])

    def test_install_commands(self):
        self.assertEqual(buildpolicy.required_files("pip install -r requirements.backend.txt", "")[0][1],
                         ["requirements.backend.txt"])
        self.assertEqual(buildpolicy.required_files("npm ci", "")[0][1], ["package.json"])


class Preflight(unittest.TestCase):
    def test_the_live_failure_is_caught_with_a_useful_message(self):
        msg = problem("flask")
        self.assertIn("the module app that the start command runs is not in the top level", msg)
        self.assertIn("backend/main.py", msg)
        self.assertIn("frontend/app.py", msg)
        self.assertIn("multi-service Docker Compose project", msg)
        self.assertIn("uvicorn backend.main:app --host 0.0.0.0 --port 8000 for a FastAPI app", msg)
        self.assertIn("gunicorn backend.main:app -b 0.0.0.0:8000 for a Flask app", msg)
        self.assertNotIn("tests/", msg)

    def test_fastapi_preset_on_the_same_repository_is_caught_too(self):
        msg = problem("fastapi")
        self.assertIn("the module main", msg)
        self.assertIn("uvicorn backend.main:app --host 0.0.0.0 --port 8000", msg)

    def test_settings_that_fit_the_repository_pass(self):
        self.assertEqual(problem("fastapi", install_command="pip install -r requirements.backend.txt",
                                 start_command="uvicorn backend.main:app --host 0.0.0.0 --port 8000"), "")
        self.assertEqual(problem("flask", files={"app.py", "requirements.txt"}), "")
        self.assertEqual(problem("flask", files={"site/app.py", "site/requirements.txt"}, root_dir="site"), "")

    def test_a_missing_requirements_file_is_named(self):
        self.assertIn("requirements.txt", problem("flask", files={"app.py"}))

    def test_unknown_repository_contents_never_block(self):
        self.assertEqual(problem("flask", files=None), "")


class StaticAndStreamlit(unittest.TestCase):
    def test_static_site_needs_an_index_html(self):
        msg = problem("static", files={"README.md", "forgeops-devops (1).zip"})     # the Devops repository
        self.assertIn("an index.html (the site's home page) is not in the top level", msg)
        self.assertEqual(problem("static", files={"index.html", "README.md"}), "")   # cohackers
        self.assertEqual(problem("static", files={"site/index.html"}, root_dir="site"), "")
        self.assertEqual(problem("static", files={"public/index.html"}, output_dir="public"), "")

    def test_built_frontends_are_not_required_to_ship_index_html(self):
        self.assertEqual(problem("react", files={"package.json", "src/main.jsx", "index.html"}), "")
        self.assertEqual(problem("react", files={"package.json", "src/main.jsx"}), "", "built by npm run build")

    def test_streamlit_preset(self):
        from kova.gitinfo import detect_framework
        self.assertEqual(detect_framework(None, {"requirements.txt", "app.py"}, "streamlit\npandas\n"), "streamlit")
        self.assertEqual(problem("streamlit", files={"app.py", "requirements.txt"}), "")
        self.assertIn("app.py", problem("streamlit", files={"frontend/app.py", "requirements.txt"}))
        text, port = dockerfiles.generate({"framework": "streamlit"})
        self.assertEqual(port, 8000)
        self.assertIn("python -c 'import streamlit' 2>/dev/null || pip install streamlit==1.40.1", text)

class EngineTests(CoolifyCase):
    """The engine refuses before creating anything in Coolify; nothing is built."""
    def setUp(self):
        super().setUp()
        self.configure_coolify()
        self.as_student()
        self.app.config["GIT_LOOKUPS"] = True
        p = mock.patch("kova.engine.resolve_commit", return_value=("d" * 40, "commit"))
        p.start()
        self.addCleanup(p.stop)

    def deploy_files(self, files, **project):
        fake = FakeCoolify()
        with mock.patch("kova.buildpolicy.repository_files", return_value=files), \
             mock.patch("kova.engine.requests.request", side_effect=fake):
            d = self.wait(self.create(**project).get_json()["deployment_id"])
        return d, fake

    def test_mismatched_preset_fails_before_the_build(self):
        d, fake = self.deploy_files(RAG_FILES, framework="flask")
        self.assertEqual(d["status"], "failed")
        self.assertIn("backend/main.py", d["error"])
        self.assertNotIn("POST", {m for m, _ in paths(fake)}, "no service was created or started")

    def test_fitting_settings_deploy(self):
        d, _ = self.deploy_files(RAG_FILES, framework="fastapi",
                                 install_command="pip install -r requirements.backend.txt",
                                 start_command="uvicorn backend.main:app --host 0.0.0.0 --port 8000")
        self.assertEqual(d["status"], "ready", d)

    def test_unreadable_file_list_builds_anyway_with_a_note(self):
        d, _ = self.deploy_files(None, framework="flask")
        self.assertEqual(d["status"], "ready", d)
        self.assertTrue(any("building anyway" in line for line in self.logs(d["id"])))


if __name__ == "__main__":
    unittest.main()
