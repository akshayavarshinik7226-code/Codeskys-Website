"""The generated deployment compose file: network isolation and input safety."""
import json
import unittest

from kova import composegen
from kova.composegen import ComposeError

SHA = "a" * 40
DEP = "dep_abcdef0123456789"


def doc(**over):
    args = dict(slug="web-app", deployment_id=DEP, repository_id=1296269,
                app_context=composegen.build_context("https://github.com/octocat/Hello-World", SHA),
                runtime_image="codeskys/runtime:abc123", secret_file=f"/srv/codeskys/runtime/{DEP}/runtime_secret",
                ingress_network="codeskys-apps", link_network="codeskys-runtime-link",
                port=3000, env_keys=["API_URL"], private_cidr="10.213.0.16/28", runtime_gid=1999,
                project_id=1, website_instance="prod")
    args.update(over)
    return composegen.generate(**args)


class NetworkIsolation(unittest.TestCase):
    def test_app_never_reaches_authz_or_the_data_api(self):
        d = doc()
        self.assertEqual(set(d["services"]["app"]["networks"]), {"ingress", "private"})
        self.assertNotIn("link", d["services"]["app"]["networks"])

    def test_sidecar_is_not_on_the_shared_ingress_network(self):
        d = doc()
        self.assertEqual(set(d["services"]["codeskys-runtime"]["networks"]), {"private", "link"})

    def test_private_network_is_internal_with_its_own_subnet_and_the_sidecar_serves_only_it(self):
        d = doc()
        net = d["networks"]["private"]
        self.assertTrue(net["internal"])
        self.assertEqual(net["ipam"]["config"][0]["subnet"], "10.213.0.16/28")
        env = d["services"]["codeskys-runtime"]["environment"]
        self.assertEqual(env["CODESKYS_RUNTIME_CLIENT_CIDRS"], "10.213.0.16/28")

    def test_external_networks_are_referenced_not_created_and_never_platform_or_control(self):
        d = doc()
        self.assertTrue(d["networks"]["ingress"]["external"])
        self.assertTrue(d["networks"]["link"]["external"])
        names = {n.get("name") for n in d["networks"].values()}
        self.assertFalse(names & {"platform", "control", "codeskys_platform", "codeskys_control"})

    def test_private_network_carries_ownership_labels(self):
        labels = doc()["networks"]["private"]["labels"]
        self.assertEqual((labels["codeskys.project_id"], labels["codeskys.website_instance"]), ("1", "prod"))
        with self.assertRaises(ComposeError):
            doc(website_instance="Prod Env")

    def test_same_network_for_ingress_and_link_is_refused(self):
        with self.assertRaises(ComposeError):
            doc(link_network="codeskys-apps")

    def test_subnets_are_deterministic_and_never_collide(self):
        seen = {composegen.private_subnet(i) for i in range(1, 4096)}
        self.assertEqual(len(seen), 4095)
        self.assertEqual(composegen.private_subnet(1), "10.213.0.16/28")
        for bad in (0, 4096, -1):
            with self.assertRaises(ComposeError):
                composegen.private_subnet(bad)
        with self.assertRaises(ComposeError):
            composegen.private_subnet(1, "8.8.0.0/16")  # public pool refused


class SecretHandling(unittest.TestCase):
    def test_secret_is_a_read_only_bind_mount_and_never_an_env_var(self):
        d = doc()
        rt = d["services"]["codeskys-runtime"]
        vol = rt["volumes"][0]
        self.assertTrue(vol["read_only"])
        self.assertEqual(vol["target"], "/run/secrets/codeskys_runtime")
        text = json.dumps(d)
        self.assertNotIn("CODESKYS_RUNTIME_SECRET\"", text)
        self.assertNotIn("secret", json.dumps(d["services"]["app"]).lower())
        self.assertEqual(rt["group_add"], ["1999"])

    def test_secret_file_must_belong_to_this_deployment(self):
        with self.assertRaises(ComposeError):
            doc(secret_file="/srv/codeskys/runtime/dep_otherdeployment1/runtime_secret")
        with self.assertRaises(ComposeError):
            doc(secret_file="relative/path")

    def test_env_values_are_never_embedded_and_reserved_names_are_dropped(self):
        d = doc(env_keys=["API_URL", "CODESKYS_DEPLOYMENT_ID", "PORT", "AUTHZ_INTERNAL_URL"])
        env = d["services"]["app"]["environment"]
        self.assertEqual(env["API_URL"], "${API_URL}")
        self.assertEqual(env["CODESKYS_DEPLOYMENT_ID"], DEP)       # platform value, not the user's
        self.assertEqual(env["PORT"], "3000")
        self.assertNotIn("AUTHZ_INTERNAL_URL", env)


class Hardening(unittest.TestCase):
    def test_both_containers_are_limited_and_unprivileged(self):
        d = doc()
        for name, svc in d["services"].items():
            self.assertEqual(svc["cap_drop"], ["ALL"], name)
            self.assertIn("no-new-privileges:true", svc["security_opt"], name)
            self.assertEqual(svc["pids_limit"], 256, name)
            self.assertIn("mem_limit", svc, name)
            self.assertNotIn("privileged", svc, name)
            self.assertNotIn("ports", svc, name)  # nothing is published on the host
            self.assertFalse(any("docker.sock" in json.dumps(v) for v in svc.get("volumes", [])), name)
        self.assertTrue(d["services"]["codeskys-runtime"]["read_only"])

    def test_build_is_pinned_to_the_exact_commit(self):
        ctx = doc()["services"]["app"]["build"]["context"]
        self.assertEqual(ctx, f"https://github.com/octocat/Hello-World.git#{SHA}")


class InputValidation(unittest.TestCase):
    def test_hostile_values_are_refused(self):
        cases = [
            dict(slug="web-app\nprivileged: true"), dict(slug="../x"), dict(slug="A" * 60),
            dict(deployment_id="dep_x; rm -rf /"), dict(repository_id=0), dict(repository_id="1"),
            dict(env_keys=["OK", "BAD KEY"]), dict(env_keys=["X=1\nprivileged: true"]),
            dict(port=8090), dict(port=0), dict(port=70000),
            dict(memory="lots"), dict(cpus="all"), dict(dockerfile="../../etc/passwd"),
            dict(runtime_image="evil image"), dict(private_cidr="0.0.0.0/0"), dict(private_cidr="8.8.8.0/28"),
            dict(private_cidr=""), dict(runtime_gid=0), dict(ingress_network="bad net"),
        ]
        for over in cases:
            with self.subTest(over=over), self.assertRaises(ComposeError):
                doc(**over)

    def test_only_pinned_github_contexts(self):
        for url, sha in (("https://gitlab.com/a/b", SHA), ("http://github.com/a/b", SHA),
                         ("https://github.com/a/b/../c", SHA), ("https://github.com/a/b", "main"),
                         ("https://github.com/a/b", "a" * 39)):
            with self.subTest(url=url, sha=sha), self.assertRaises(ComposeError):
                composegen.build_context(url, sha)
        self.assertEqual(composegen.build_context("https://github.com/a/b.git", SHA),
                         f"https://github.com/a/b.git#{SHA}")

    def test_output_is_valid_json(self):
        json.loads(composegen.render(doc()))
