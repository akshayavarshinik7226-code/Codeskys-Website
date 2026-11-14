"""Repository links: a URL that passes validation must be one the deployment engine can build.

GitHub folder links (``/tree/<branch>/<dir>``) used to be accepted as a generic Git URL and
then refused by the Coolify engine. They are now resolved into branch + root directory, with
branch names that contain slashes matched against the repository's real branches."""
import unittest
from unittest import mock

import requests

from kova import composegen
from kova.gitinfo import GitError, clean_root_dir, parse_repo, resolve_tree

from .test_coolify import CoolifyCase
from .test_platform import PlatformCase


class _R:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def fake_github(branches):
    """``_gh`` stand-in: answers branch lookups for the given branch names, 404 otherwise."""
    def gh(path, timeout, token=None):
        prefix = "/repos/acme/web/branches/"
        if path.startswith(prefix):
            return _R(200, {"name": path[len(prefix):]}) if path[len(prefix):] in branches else _R(404)
        return _R(404)
    return gh


class ParseTests(unittest.TestCase):
    def test_plain_repository_is_unchanged(self):
        info = parse_repo("https://github.com/acme/web.git")
        self.assertEqual(info["url"], "https://github.com/acme/web")
        self.assertNotIn("tree_path", info)

    def test_folder_link_is_a_github_repository_with_a_tree_path(self):
        info = parse_repo("https://github.com/acme/web/tree/feature/login/apps/site/")
        self.assertEqual((info["provider"], info["url"], info["tree_path"]),
                         ("github", "https://github.com/acme/web", "feature/login/apps/site"))

    def test_file_link_and_traversal_are_refused(self):
        with self.assertRaises(GitError):
            parse_repo("https://github.com/acme/web/blob/main/index.html")
        with self.assertRaises(GitError):
            parse_repo("https://github.com/acme/web/tree/main/../secrets")

    def test_root_dir_rule_matches_the_engine(self):
        self.assertEqual(clean_root_dir("./apps/web/"), "apps/web")
        self.assertEqual(clean_root_dir("."), "")
        for bad in ("../etc", "a b", "apps/../..", "x;rm"):
            with self.assertRaises(GitError):
                clean_root_dir(bad)
        # Whatever validation accepts, the engine's build context accepts too.
        for good in ("apps/web", "site", "a.b/c_d-e"):
            composegen.build_context("https://github.com/acme/web", "a" * 40, clean_root_dir(good))


class DetectionTests(unittest.TestCase):
    def test_python_frameworks_are_told_apart_by_requirements(self):
        from kova.gitinfo import detect_framework
        files = {"requirements.txt", "main.py"}
        self.assertEqual(detect_framework(None, files, "fastapi==0.110\nuvicorn[standard]>=0.29  # ASGI\n"), "fastapi")
        self.assertEqual(detect_framework(None, files, "Flask==3.0\n"), "flask")
        self.assertEqual(detect_framework(None, files, "flask\nfastapi\n"), "flask")
        self.assertEqual(detect_framework(None, files | {"manage.py"}, "fastapi"), "django")
        self.assertEqual(detect_framework(None, files), "flask", "no requirements text: previous behaviour")

class ResolveTests(unittest.TestCase):
    def resolve(self, path, branches, **kw):
        with mock.patch("kova.gitinfo._gh", side_effect=fake_github(branches)):
            return resolve_tree(parse_repo(f"https://github.com/acme/web/tree/{path}"), **kw)

    def test_branch_with_slashes_and_subdirectory(self):
        self.assertEqual(self.resolve("feature/login/apps/web", {"main", "feature/login"}),
                         ("feature/login", "apps/web"))

    def test_longest_matching_branch_wins(self):
        self.assertEqual(self.resolve("release/v2/docs", {"release", "release/v2"}), ("release/v2", "docs"))

    def test_branch_only(self):
        self.assertEqual(self.resolve("main", {"main"}), ("main", ""))

    def test_unknown_branch_is_refused(self):
        with self.assertRaises(GitError):
            self.resolve("nope/site", {"main"})

    def test_github_unreachable_is_refused_not_guessed(self):
        with mock.patch("kova.gitinfo._gh", side_effect=requests.ConnectionError()):
            with self.assertRaises(GitError):
                resolve_tree(parse_repo("https://github.com/acme/web/tree/main/site"))

    def test_without_lookups_only_an_unambiguous_link_is_accepted(self):
        info = parse_repo("https://github.com/acme/web/tree/main")
        self.assertEqual(resolve_tree(info, lookups=False), ("main", ""))
        with self.assertRaises(GitError):
            resolve_tree(parse_repo("https://github.com/acme/web/tree/main/site"), lookups=False)


class ProjectApiTests(PlatformCase):
    def setUp(self):
        super().setUp()
        self.as_student()

    def test_folder_link_sets_branch_and_root_directory(self):
        self.app.config["GIT_LOOKUPS"] = True
        with mock.patch("kova.gitinfo._gh", side_effect=fake_github({"main", "feature/login"})):
            r = self.create(repo_url="https://github.com/acme/web/tree/feature/login/apps/web", deploy=False)
        self.assertEqual(r.status_code, 201, r.get_json())
        p = r.get_json()["project"]
        self.assertEqual((p["repo_url"], p["branch"], p["root_dir"]),
                         ("https://github.com/acme/web", "feature/login", "apps/web"))

    def test_ambiguous_folder_link_without_lookups_is_refused_at_creation(self):
        r = self.create(repo_url="https://github.com/acme/web/tree/main/site", deploy=False)
        self.assertEqual(r.status_code, 422)
        self.assertIn("root directory", r.get_json()["error"])

    def test_invalid_root_directory_is_refused(self):
        self.assertEqual(self.create(root_dir="../etc", deploy=False).status_code, 422)
        slug = self.create(deploy=False).get_json()["project"]["slug"]
        self.assertEqual(self.call("patch", f"/api/projects/{slug}", {"root_dir": "a b"}).status_code, 422)
        r = self.call("patch", f"/api/projects/{slug}", {"root_dir": "./apps/web"})
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_reserved_variable_names_are_refused(self):
        r = self.create(deploy=False, env=[{"key": "CODESKYS_RUNTIME_TOKEN", "value": "x"}])
        self.assertEqual(r.status_code, 422)
        self.assertIn("set by Codeskys", r.get_json()["error"])
        slug = self.create(deploy=False).get_json()["project"]["slug"]
        for key in ("CODESKYS_RUNTIME_URL", "PORT", "DATA_API_URL"):
            r = self.call("post", f"/api/projects/{slug}/env", {"key": key, "value": "v"})
            self.assertEqual(r.status_code, 400, key)
        self.assertEqual(self.call("post", f"/api/projects/{slug}/env", {"key": "API_BASE", "value": "v"}).status_code,
                         200)


class CoolifyRepoTests(CoolifyCase):
    def setUp(self):
        super().setUp()
        self.configure_coolify()
        self.as_student()

    def test_non_github_repository_is_refused_when_the_engine_cannot_build_it(self):
        r = self.create(repo_url="https://gitlab.com/acme/web", deploy=False)
        self.assertEqual(r.status_code, 422)
        self.assertIn("GitHub", r.get_json()["error"])
        r = self.call("get", "/api/git/inspect?url=https://git.example.org/acme/web.git")
        self.assertEqual(r.status_code, 422)

    def test_github_repository_is_accepted(self):
        self.assertEqual(self.create(deploy=False).status_code, 201)



class DuplicateRequestTests(PlatformCase):
    """A double click or a re-delivered webhook returns the deployment already in flight."""
    def setUp(self):
        super().setUp()
        self.as_student()
        self.app.config["GIT_LOOKUPS"] = True
        for p in (mock.patch("kova.engine.resolve_commit", return_value=("b" * 40, "same commit")),
                  mock.patch("kova.engine.enqueue_deploy")):          # keep it queued
            p.start()
            self.addCleanup(p.stop)

    def test_same_commit_twice_is_one_deployment(self):
        slug = self.create(deploy=False).get_json()["project"]["slug"]
        a = self.call("post", f"/api/projects/{slug}/deploy").get_json()["deployment"]["id"]
        b = self.call("post", f"/api/projects/{slug}/deploy").get_json()["deployment"]["id"]
        self.assertEqual(a, b)
        self.assertEqual(self.app_conn().execute("SELECT COUNT(*) FROM deployments").fetchone()[0], 1)
        # A different target is a different deployment.
        c = self.call("post", f"/api/projects/{slug}/deploy", {"target": "preview"}).get_json()["deployment"]["id"]
        self.assertNotEqual(a, c)


class CommitResolutionTests(unittest.TestCase):
    """A Coolify build is pinned to an exact commit; GitHub's API budget (60/h per server) can run out."""
    @staticmethod
    def _pkt(payload: bytes) -> bytes:
        return b"%04x" % (len(payload) + 4) + payload

    INFO_REFS = (_pkt.__func__(b"# service=git-upload-pack\n") + b"0000"
                 + _pkt.__func__(b"a" * 40 + b" HEAD\0multi_ack thin-pack symref=HEAD:refs/heads/main\n")
                 + _pkt.__func__(b"b" * 40 + b" refs/heads/main\n")
                 + _pkt.__func__(b"c" * 40 + b" refs/heads/feature/login\n")
                 + b"0000")

    def test_branch_head_reads_git_protocol_refs(self):
        from kova import gitinfo
        with mock.patch("kova.gitinfo.requests.get", return_value=_Raw(200, self.INFO_REFS)) as get:
            self.assertEqual(gitinfo.branch_head("acme/web", "main"), "b" * 40)
            self.assertEqual(gitinfo.branch_head("acme/web", "feature/login"), "c" * 40)
            self.assertIsNone(gitinfo.branch_head("acme/web", "nope"))
        self.assertIn("/acme/web.git/info/refs", get.call_args[0][0])
        self.assertIsNone(gitinfo.branch_head("../etc", "main"), "names are validated before any request")

    def test_rate_limited_api_falls_back_to_git_protocol(self):
        from kova import gitinfo
        def get(url, **kw):
            return _Raw(403, b"{}") if "api.github.com" in url else _Raw(200, self.INFO_REFS)
        with mock.patch("kova.gitinfo.requests.get", side_effect=get):
            self.assertEqual(gitinfo.latest_commit("acme/web", "main")["sha"], "b" * 40)

    def test_platform_token_is_used_for_api_calls_when_set(self):
        from kova import gitinfo
        with mock.patch.dict("os.environ", {"KOVA_GITHUB_API_TOKEN": "github_pat_TEST"}),              mock.patch("kova.gitinfo.requests.get", return_value=_Raw(404, b"{}")) as get:
            gitinfo._gh("/repos/acme/web", 5)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer github_pat_TEST")
        with mock.patch.dict("os.environ", {}, clear=True),              mock.patch("kova.gitinfo.requests.get", return_value=_Raw(404, b"{}")) as get:
            gitinfo._gh("/repos/acme/web", 5)
        self.assertNotIn("Authorization", get.call_args.kwargs["headers"])


class _Raw:
    def __init__(self, status, content):
        self.status_code, self.content = status, content

    def json(self):
        import json
        return json.loads(self.content or b"{}")


class NoPlaceholderCommitTests(CoolifyCase):
    def setUp(self):
        super().setUp()
        self.configure_coolify()
        self.as_student()
        self.app.config["GIT_LOOKUPS"] = True

    def test_unknown_branch_creates_the_project_but_no_build(self):
        with mock.patch("kova.engine.latest_commit", return_value=None):
            r = self.create(branch="no-such-branch")
        self.assertEqual(r.status_code, 201)
        body = r.get_json()
        self.assertIsNone(body["deployment_id"])
        self.assertIn("could not find branch 'no-such-branch'", body["deploy_error"])
        self.assertEqual(self.app_conn().execute("SELECT COUNT(*) FROM deployments").fetchone()[0], 0)
        with mock.patch("kova.engine.latest_commit", return_value=None):
            r = self.call("post", f"/api/projects/{body['project']['slug']}/deploy")
        self.assertEqual(r.status_code, 409)

    def test_resolved_commit_is_the_one_built(self):
        with mock.patch("kova.engine.latest_commit", return_value={"sha": "e" * 40, "message": "m"}),              mock.patch("kova.engine.enqueue_deploy"):
            r = self.create()
        dep = self.app_conn().execute("SELECT commit_sha FROM deployments WHERE id = ?",
                                      (r.get_json()["deployment_id"],)).fetchone()
        self.assertEqual(dep[0], "e" * 40)

if __name__ == "__main__":
    unittest.main()
