"""Build isolation policy: Dockerfile checks, the Dockerfile fetch, and the Coolify
service check (kova/buildpolicy.py). Engine-level behaviour is in
test_coolify.BuildIsolationTests."""
import io
import json
import unittest
from unittest import mock

import requests

from kova import buildpolicy, composegen
from kova.buildpolicy import PolicyError, coolify_service_problems, dockerfile_problems

SHA = "b" * 40
DEP = "dep_abcdef0123456789"


def compose():
    return composegen.generate_service(
        slug="web-app", deployment=7, port=8080,
        app_context=composegen.build_context("https://github.com/octocat/Hello-World", SHA))


def sidecar_compose():
    return composegen.generate(
        slug="web-app", deployment_id=DEP, repository_id=1,
        app_context=composegen.build_context("https://github.com/octocat/Hello-World", SHA),
        runtime_image="codeskys/runtime:abc123", secret_file=f"/srv/codeskys/runtime-secrets/{DEP}/runtime_secret",
        ingress_network="codeskys-apps", link_network="codeskys-runtime-link", private_cidr="10.213.0.16/28",
        runtime_gid=1997, project_id=1)


class DockerfileRules(unittest.TestCase):
    def test_ordinary_dockerfiles_pass(self):
        for text in ("FROM node:20-alpine\nWORKDIR /app\nCOPY . .\nRUN npm ci && npm run build\nCMD [\"npm\", \"start\"]\n",
                     "# a comment mentioning --network=host is fine\nFROM python:3.12-slim AS base\n"
                     "RUN --mount=type=cache,target=/root/.cache pip install flask\n",
                     "FROM alpine:3.20\nRUN apk add curl \\\n    git\n"):
            with self.subTest(text=text[:30]):
                self.assertEqual(dockerfile_problems(text), [])

    def test_host_network_insecure_mode_and_devices_are_refused(self):
        for text, needle in (
            ("FROM alpine\nRUN --network=host wget http://127.0.0.1:8000\n", "--network"),
            ("FROM alpine\nRUN --network host true\n", "--network"),
            ("FROM alpine\nrun --NETWORK=HOST true\n", "--NETWORK"),
            ("FROM alpine\nRUN --security=insecure true\n", "--security"),
            ("FROM alpine\nRUN --device=/dev/fuse true\n", "--device"),
            ("FROM alpine\nONBUILD RUN --network=host true\n", "--network"),
            ("FROM alpine\nRUN --net\\\nwork=host true\n", "--network"),               # continuation
            ("FROM alpine\nRUN --net\\  \r\nwork=host true\n", "--network"),           # CRLF + spaces
            ("FROM alpine\nRUN --net\\\n# hidden\nwork=host true\n", "--network"),     # comment inside it
        ):
            with self.subTest(text=text):
                problems = dockerfile_problems(text)
                self.assertTrue(problems)
                self.assertIn(needle, " ".join(problems))

    def test_parser_directives_are_refused(self):
        for directive in ("# syntax=docker/dockerfile:1", "#syntax=evil.example/x", "# escape=`", "# check=skip=all"):
            with self.subTest(directive=directive):
                self.assertTrue(dockerfile_problems(f"{directive}\nFROM alpine\n"))

    def test_a_dockerfile_without_from_is_refused(self):
        self.assertTrue(dockerfile_problems("other/Dockerfile"))     # what a symlink looks like over raw HTTP


class _Stream(io.BytesIO):
    def read(self, size=-1, decode_content=False):          # urllib3's signature
        return super().read(size)


class _Raw:
    def __init__(self, status, body=b""):
        self.status_code, self.raw = status, _Stream(body)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class DockerfileFetch(unittest.TestCase):
    def fetch(self, response, repo="https://github.com/octocat/Hello-World", sha=SHA):
        with mock.patch("kova.buildpolicy.requests.get", return_value=response) as get:
            try:
                return buildpolicy.fetch_dockerfile(repo, sha), get
            except PolicyError as exc:
                return exc, get

    def test_reads_the_exact_commit_without_following_redirects(self):
        text, get = self.fetch(_Raw(200, b"FROM alpine\n"))
        self.assertEqual(text, "FROM alpine\n")
        url = get.call_args[0][0]
        self.assertEqual(url, f"https://raw.githubusercontent.com/octocat/Hello-World/{SHA}/Dockerfile")
        self.assertFalse(get.call_args[1]["allow_redirects"])

    def test_missing_oversized_and_unreachable_fail_closed(self):
        self.assertIn("no Dockerfile", str(self.fetch(_Raw(404))[0]))
        self.assertIn("HTTP 302", str(self.fetch(_Raw(302))[0]))
        big = b"FROM alpine\n" + b"#" * buildpolicy.MAX_DOCKERFILE_BYTES
        self.assertIn("256 KB", str(self.fetch(_Raw(200, big))[0]))
        with mock.patch("kova.buildpolicy.requests.get", side_effect=requests.ConnectionError("x")):
            with self.assertRaises(PolicyError):
                buildpolicy.fetch_dockerfile("https://github.com/octocat/Hello-World", SHA)

    def test_only_github_repositories_at_a_full_commit(self):
        for repo, sha in (("https://gitlab.com/a/b", SHA), ("https://github.com/a/b", "main"),
                          ("https://github.com/a/b/../../x", SHA)):
            with self.subTest(repo=repo, sha=sha), mock.patch("kova.buildpolicy.requests.get") as get:
                with self.assertRaises(PolicyError):
                    buildpolicy.fetch_dockerfile(repo, sha)
                get.assert_not_called()


class CoolifyServiceCheck(unittest.TestCase):
    """Fields of GET /services/{uuid} (Coolify 4.7.5)."""

    def service(self, sensitive=True, **over):
        svc = {"name": "web-app", "service_type": None, "connect_to_docker_network": False,
               "applications": [{"name": "app", "fqdn": "http://web-app.apps.example.com:8080"}], "databases": []}
        if sensitive:                               # only a token with read:sensitive sees the compose file
            svc["docker_compose_raw"] = json.dumps(compose())
        return {**svc, **over}

    def test_the_generated_service_passes(self):
        self.assertEqual(coolify_service_problems(self.service(), compose()), [])
        self.assertEqual(coolify_service_problems(self.service()), [])          # start/restart: structure only
        self.assertEqual(coolify_service_problems(self.service(sensitive=False), compose()), [],
                         "without read:sensitive the compose file is hidden and cannot be compared")

    def test_template_services_and_the_destination_network_are_refused(self):
        self.assertIn("template service", " ".join(coolify_service_problems(self.service(service_type="wordpress"))))
        self.assertIn("destination's Docker network",
                      " ".join(coolify_service_problems(self.service(connect_to_docker_network=True))))

    def test_any_container_besides_app_is_refused(self):
        for apps, dbs in (([{"name": "app"}, {"name": "worker"}], []), ([{"name": "web"}], []), ([], []),
                          ([{"name": "app"}], [{"name": "postgres"}])):
            with self.subTest(apps=apps, dbs=dbs):
                self.assertIn("not one 'app'", " ".join(coolify_service_problems(
                    self.service(applications=apps, databases=dbs))))

    def test_a_different_or_unsafe_stored_compose_is_refused(self):
        other = compose()
        other["services"]["app"]["environment"]["X"] = "1"
        self.assertIn("not the one Codeskys generated",
                      " ".join(coolify_service_problems(self.service(docker_compose_raw=json.dumps(other)), compose())))
        for raw in ("", "not json", "services:\n  app:\n    network_mode: host\n", json.dumps({"services": {}}),
                    json.dumps(sidecar_compose())):
            with self.subTest(raw=raw[:20]):
                self.assertIn("not a safe Codeskys compose file",
                              " ".join(coolify_service_problems(self.service(docker_compose_raw=raw))))
