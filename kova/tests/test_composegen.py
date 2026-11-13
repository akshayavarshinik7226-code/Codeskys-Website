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
                port=3000, env_keys=["API_URL"], private_cidr="10.213.0.16/28", runtime_gid=1997,
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
        self.assertEqual(rt["group_add"], ["1997"])

    def test_sidecar_mounts_only_its_own_secret_file(self):
        rt = doc()["services"]["codeskys-runtime"]
        self.assertEqual([v["source"] for v in rt["volumes"]], [f"/srv/codeskys/runtime/{DEP}/runtime_secret"])
        self.assertNotIn("/etc/codeskys", json.dumps(doc()))

    def test_platform_secret_groups_are_never_given_to_the_sidecar(self):
        # 1998 guards the GitHub App private key, 1999 the platform runtime secret.
        for gid in (0, 1998, 1999):
            with self.subTest(gid=gid), self.assertRaises(ComposeError):
                doc(runtime_gid=gid)

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


class BuildAndRuntimeAllowlist(unittest.TestCase):
    """assert_safe: nothing beyond what generate() writes may reach Coolify. Each
    mutation below would give the build or a container host networking,
    privileges, devices or host access (verified for build.network/entitlements
    with Docker 29.8 / Compose 5.5 on 2026-11-12)."""

    def mutated(self, fn):
        d = doc()
        fn(d)
        return d

    def test_generated_documents_pass(self):
        d = doc()
        self.assertIs(composegen.assert_safe(d), d)

    def test_build_may_only_set_context_and_dockerfile(self):
        for key, value in (("network", "host"), ("entitlements", ["network.host"]), ("privileged", True),
                           ("extra_hosts", ["host.docker.internal:host-gateway"]), ("ssh", ["default"]),
                           ("secrets", ["x"]), ("args", {"A": "1"}), ("dockerfile_inline", "FROM x"),
                           ("additional_contexts", {"x": "/"}), ("network", "default")):
            with self.subTest(key=key), self.assertRaises(ComposeError):
                composegen.assert_safe(self.mutated(lambda d: d["services"]["app"]["build"].__setitem__(key, value)))

    def test_no_host_namespaces_privileges_devices_or_mounts(self):
        for svc in ("app", "codeskys-runtime"):
            for key, value in (("network_mode", "host"), ("privileged", True), ("pid", "host"), ("ipc", "host"),
                               ("userns_mode", "host"), ("cap_add", ["SYS_ADMIN"]), ("devices", ["/dev/kvm"]),
                               ("sysctls", {"net.ipv4.ip_forward": 1}), ("extra_hosts", ["h:host-gateway"]),
                               ("volumes_from", ["x"]), ("cgroup_parent", "/"), ("runtime", "runc"),
                               ("cap_drop", []), ("security_opt", ["seccomp=unconfined"])):
                with self.subTest(svc=svc, key=key), self.assertRaises(ComposeError):
                    composegen.assert_safe(self.mutated(lambda d: d["services"][svc].__setitem__(key, value)))
        mounts = ([{"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}],
                  [{"type": "bind", "source": "/etc/codeskys", "target": "/run/secrets/codeskys_runtime",
                    "read_only": True, "bind": {"propagation": "rshared"}}])
        for vols in mounts:
            with self.subTest(vols=vols), self.assertRaises(ComposeError):
                composegen.assert_safe(self.mutated(lambda d: d["services"]["app"].__setitem__("volumes", vols)))
        with self.assertRaises(ComposeError):   # sidecar mount made writable
            composegen.assert_safe(self.mutated(
                lambda d: d["services"]["codeskys-runtime"]["volumes"][0].__setitem__("read_only", False)))

    def test_no_extra_services_networks_or_reserved_network_names(self):
        cases = [
            lambda d: d["services"].__setitem__("evil", {"image": "x", "network_mode": "host"}),
            lambda d: d.__setitem__("volumes", {"x": {}}),
            lambda d: d["networks"].__setitem__("platform", {"name": "codeskys_platform", "external": True}),
            lambda d: d["networks"]["ingress"].__setitem__("name", "host"),
            lambda d: d["networks"]["link"].__setitem__("name", "codeskys_platform"),
            lambda d: d["networks"]["ingress"].__setitem__("name", "coolify"),
            lambda d: d["networks"]["private"].__setitem__("internal", False),
            lambda d: d["networks"]["private"].__setitem__("driver", "host"),
            lambda d: d["services"]["app"]["networks"].__setitem__("link", {}),
            lambda d: d["services"]["codeskys-runtime"].__setitem__("group_add", ["1998"]),
        ]
        for i, fn in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(ComposeError):
                composegen.assert_safe(self.mutated(fn))

    def test_reserved_networks_are_refused_at_generation(self):
        for name in ("host", "bridge", "none", "default", "coolify", "codeskys_platform", "codeskys_control"):
            for field in ("ingress_network", "link_network"):
                with self.subTest(name=name, field=field), self.assertRaises(ComposeError):
                    doc(**{field: name})


def service_doc(**over):
    args = dict(slug="web-app", deployment=7, port=3000, env_keys=["API_URL"],
                app_context=composegen.build_context("https://github.com/octocat/Hello-World", SHA))
    args.update(over)
    return composegen.generate_service(**args)


class CoolifyServiceCompose(unittest.TestCase):
    """The compose file a Coolify 4.7.5 service gets (generate_service)."""

    def test_the_app_runs_alone_with_no_networks_of_its_own(self):
        d = service_doc()
        self.assertEqual(set(d), {"services"})
        self.assertEqual(set(d["services"]), {"app"})
        app = d["services"]["app"]
        for key in ("networks", "network_mode", "volumes", "ports", "depends_on"):
            self.assertNotIn(key, app)
        self.assertEqual(app["expose"], ["3000"])
        self.assertEqual(app["cap_drop"], ["ALL"])
        self.assertEqual(app["security_opt"], ["no-new-privileges:true"])
        self.assertEqual(app["environment"], {"API_URL": "${API_URL}", "PORT": "3000"})
        json.loads(composegen.render(d))

    def test_reserved_and_coolify_magic_names_are_dropped(self):
        env = service_doc(env_keys=["API_URL", "SERVICE_FQDN_APP", "service_password_x", "COOLIFY_URL",
                                    "CODESKYS_RUNTIME_SECRET", "PORT"])["services"]["app"]["environment"]
        self.assertEqual(env, {"API_URL": "${API_URL}", "PORT": "3000"})

    def test_only_pinned_github_contexts(self):
        for ctx in ("/srv/local/app", "https://github.com/a/b.git", "https://gitlab.com/a/b.git#" + SHA):
            with self.subTest(ctx=ctx), self.assertRaises(ComposeError):
                service_doc(app_context=ctx)

    def test_anything_that_could_join_a_network_or_the_host_is_refused(self):
        for key, value in (("networks", {"default": {}}), ("network_mode", "host"), ("privileged", True),
                           ("volumes", ["/:/host"]), ("ports", ["8000:8000"]), ("cap_add", ["NET_ADMIN"]),
                           ("pid", "host"), ("extra_hosts", ["x:1.2.3.4"])):
            with self.subTest(key=key):
                d = service_doc()
                d["services"]["app"][key] = value
                with self.assertRaises(ComposeError):
                    composegen.assert_safe_service(d)
        d = service_doc()
        d["networks"] = {"codeskys-runtime-link": {"external": True}}
        with self.assertRaises(ComposeError):
            composegen.assert_safe_service(d)
        d = service_doc()
        d["services"]["miner"] = {"image": "x"}
        with self.assertRaises(ComposeError):
            composegen.assert_safe_service(d)
        d = service_doc()
        d["services"]["app"]["build"]["network"] = "host"
        with self.assertRaises(ComposeError):
            composegen.assert_safe_service(d)

    def test_invalid_inputs_are_refused(self):
        for over in ({"slug": "Bad Slug"}, {"deployment": 0}, {"port": 0}, {"memory": "lots"}, {"cpus": "1;rm"},
                     {"env_keys": ["BAD-KEY"]}, {"dockerfile": "../Dockerfile"}):
            with self.subTest(over=over), self.assertRaises(ComposeError):
                service_doc(**over)
