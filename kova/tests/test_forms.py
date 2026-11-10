"""Project-scoped form builder and public submission endpoint tests."""
from .base import AppTestCase, STUDENT
from kova.db import connect
from kova.users import create_user


class ProjectFormsTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.login(STUDENT[0], STUDENT[2])
        r = self.post("/api/projects", {
            "name": "Form Demo",
            "repo_url": "https://github.com/acme/form-demo",
            "framework": "static",
            "deploy": False,
        })
        self.assertEqual(r.status_code, 201, r.get_json())
        self.project_slug = r.get_json()["project"]["slug"]
        self.fields = [
            {"name": "email", "label": "Email", "type": "email", "required": True, "max_length": 254},
            {"name": "message", "label": "Message", "type": "textarea", "required": True, "max_length": 2000},
        ]

    def call(self, method, path, payload=None, client=None):
        c = client or self.client
        headers = {"X-CSRF-Token": self.csrf(c)}
        return getattr(c, method)(path, json=payload or {}, headers=headers)

    def create_form(self):
        return self.call("post", f"/api/projects/{self.project_slug}/forms", {
            "name": "Contact Form", "fields": self.fields,
        })

    def test_create_and_list_project_form(self):
        created = self.create_form()
        self.assertEqual(created.status_code, 201, created.get_json())
        form = created.get_json()["form"]
        self.assertTrue(form["public_key"])
        self.assertEqual(form["endpoint"], f"/api/public/forms/{form['public_key']}")
        listed = self.client.get(f"/api/projects/{self.project_slug}/forms").get_json()["forms"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "Contact Form")

    def test_public_submission_is_validated_and_saved(self):
        form = self.create_form().get_json()["form"]
        public = self.app.test_client()
        bad = public.post(form["endpoint"], json={"email": "not-an-email", "message": "Hello"})
        self.assertEqual(bad.status_code, 422)
        good = public.post(form["endpoint"], json={"email": "student@example.test", "message": "Hello Codeskys"})
        self.assertEqual(good.status_code, 200, good.get_json())
        inbox = self.client.get(f"/api/projects/{self.project_slug}/forms/{form['id']}/submissions")
        self.assertEqual(inbox.status_code, 200)
        self.assertEqual(inbox.get_json()["total"], 1)
        self.assertEqual(inbox.get_json()["submissions"][0]["data"]["message"], "Hello Codeskys")

    def test_invalid_field_schema_is_rejected(self):
        r = self.call("post", f"/api/projects/{self.project_slug}/forms", {
            "name": "Bad Form", "fields": [{"name": "bad key", "label": "Bad", "type": "text"}],
        })
        self.assertEqual(r.status_code, 400)

    def test_paused_form_rejects_public_submissions(self):
        form = self.create_form().get_json()["form"]
        updated = self.call("patch", f"/api/projects/{self.project_slug}/forms/{form['id']}", {"is_active": False})
        self.assertEqual(updated.status_code, 200)
        public = self.app.test_client()
        r = public.post(form["endpoint"], json={"email": "student@example.test", "message": "Hello"})
        self.assertEqual(r.status_code, 404)

    def test_another_student_cannot_manage_project_forms(self):
        self.create_form()
        conn = connect(self.app.config["DATABASE"])
        other_id = create_user(conn, "other@example.test", "Other Student", "other-student-pass-123")
        conn.close()
        other = self.app.test_client()
        self.login("other@example.test", "other-student-pass-123", client=other)
        r = other.get(f"/api/projects/{self.project_slug}/forms")
        self.assertEqual(r.status_code, 404)

    def test_delete_form_removes_its_submissions(self):
        form = self.create_form().get_json()["form"]
        public = self.app.test_client()
        public.post(form["endpoint"], json={"email": "student@example.test", "message": "Hello"})
        deleted = self.call("delete", f"/api/projects/{self.project_slug}/forms/{form['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(public.post(form["endpoint"], json={"email": "student@example.test", "message": "Again"}).status_code, 404)
