import os
import shutil
import tempfile
import unittest

os.environ.pop("KOVA_ADMIN_EMAIL", None)
os.environ.pop("KOVA_ADMIN_PASSWORD", None)

from kova import create_app
from kova.db import connect
from kova.users import create_user

ADMIN = ("admin@example.test", "Ada Admin", "correct-horse-battery")
STUDENT = ("sam@example.test", "Sam Student", "student-pass-123456")


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Keep each test's generated keys and instance files out of the repo.
        os.environ["KOVA_INSTANCE_DIR"] = self.tmp
        self.app = create_app({
            "TESTING": True,
            "DATABASE": os.path.join(self.tmp, "t.db"),
            "SECRET_KEY": "test-secret",
            "COOLIFY_URL": "", "COOLIFY_TOKEN": "",
        })
        conn = connect(self.app.config["DATABASE"])
        self.admin_id = create_user(conn, *ADMIN, role="admin")
        self.student_id = create_user(conn, *STUDENT, role="student")
        conn.close()
        self.client = self.app.test_client()

    def tearDown(self):
        os.environ.pop("KOVA_INSTANCE_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def csrf(self, client=None):
        return (client or self.client).get("/api/auth/me").get_json()["csrf"]

    def post(self, path, payload=None, client=None, token=None):
        c = client or self.client
        token = token if token is not None else self.csrf(c)
        return c.post(path, json=payload or {}, headers={"X-CSRF-Token": token})

    def login(self, email, password, client=None):
        return self.post("/api/auth/login", {"email": email, "password": password}, client=client)
