"""Updates must show up at once: static files carry a version and are never served stale."""
import re

from .base import AppTestCase


class AssetVersionTests(AppTestCase):
    def test_pages_reference_versioned_static_files(self):
        html = self.client.get("/login").get_data(as_text=True)
        urls = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
        self.assertTrue(urls)
        self.assertTrue(all(re.search(r"\?v=[0-9a-f]{10}$", u) for u in urls), urls)

    def test_versioned_files_are_cached_for_good_and_unversioned_ones_always_revalidate(self):
        html = self.client.get("/login").get_data(as_text=True)
        versioned = re.search(r'src="(/static/js/login\.js\?v=[0-9a-f]+)"', html).group(1)
        r = self.client.get(versioned)
        self.assertEqual(r.status_code, 200)
        self.assertIn("immutable", r.headers["Cache-Control"])
        r2 = self.client.get("/static/js/login.js")
        self.assertEqual(r2.headers["Cache-Control"], "no-cache")

    def test_the_version_changes_when_a_static_file_changes(self):
        import os
        from kova import _asset_version
        before = _asset_version(self.app)
        path = os.path.join(self.app.root_path, "static", "js", "zz_version_probe.js")
        try:
            with open(path, "w") as f:
                f.write("// probe")
            self.assertNotEqual(before, _asset_version(self.app))
        finally:
            os.remove(path)
        self.assertEqual(before, _asset_version(self.app))

    def test_administrators_can_see_the_build_id_students_cannot(self):
        from .base import ADMIN, STUDENT
        self.login(STUDENT[0], STUDENT[2])
        self.assertIsNone(self.client.get("/api/platform/status").get_json().get("build"))
        admin = self.app.test_client()
        self.login(ADMIN[0], ADMIN[2], client=admin)
        self.assertRegex(admin.get("/api/platform/status").get_json()["build"], r"^[0-9a-f]{10}$")
