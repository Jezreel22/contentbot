"""Offline HTTP integration tests for the Python dashboard and approval APIs."""

from datetime import timedelta
import hashlib
import hmac
import json
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient
import httpx

from app import create_app
from core import iso, utcnow


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="contentbot-api-test-")
        self.addCleanup(self.directory.cleanup)
        self.requests = []
        self.settings = {
            "APP_PASSWORD": "test-dashboard-password",
            "X_ACCESS_TOKEN": "test-x-token",
            "WHATSAPP_ACCESS_TOKEN": "test-whatsapp-token",
            "WHATSAPP_PHONE_NUMBER_ID": "test-business-phone",
            "WHATSAPP_RECIPIENT": "2340000000000",
            "WHATSAPP_APP_SECRET": "test-whatsapp-secret",
            "WHATSAPP_VERIFY_TOKEN": "test-webhook-verification",
            "WHATSAPP_TEMPLATE_NAME": "test_approval_template",
        }

        def handle(request):
            self.requests.append(request)
            if request.url.host == "api.x.com" and request.url.path == "/2/tweets":
                return httpx.Response(201, json={"data": {"id": "mock-published-post"}})
            raise AssertionError(f"Unexpected external request: {request.url.host}")

        self.application = create_app(
            data_dir=self.directory.name,
            env=self.settings,
            transport=httpx.MockTransport(handle),
            run_worker=False,
        )
        self.client = TestClient(
            self.application,
            headers={"x-app-password": self.settings["APP_PASSWORD"]},
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def create_post(self, **overrides):
        body = {"platform": "x", "content": "A useful original tech tip", "title": "Tech tip"}
        body.update(overrides)
        response = self.client.post("/api/posts", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def future(self):
        return iso(utcnow() + timedelta(hours=1))

    def saved_post(self, post_id):
        return self.application.state.store.get(post_id)

    def approve(self, post):
        return self.client.post(f"/api/posts/{post['id']}/approve", json={"revision": post["revision"]})

    def callback(self, post, *, action="approve", sender=None, phone=None, message_id="message-one", signature=True):
        payload = {
            "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": phone or self.settings["WHATSAPP_PHONE_NUMBER_ID"]},
                "messages": [{
                    "id": message_id,
                    "from": sender or self.settings["WHATSAPP_RECIPIENT"],
                    "button": {"payload": f"{action}:{post['id']}:{post['revision']}"},
                }],
            }}]}],
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signed = "sha256=" + hmac.new(
            self.settings["WHATSAPP_APP_SECRET"].encode(), raw, hashlib.sha256
        ).hexdigest()
        return self.client.post(
            "/webhooks/whatsapp", content=raw,
            headers={"content-type": "application/json", "x-hub-signature-256": signed if signature else "forged"},
        )

    def test_password_required_and_status_does_not_expose_tokens(self):
        request = self.client.build_request("GET", "/api/status")
        del request.headers["x-app-password"]
        self.assertEqual(self.client.send(request).status_code, 401)
        self.assertEqual(self.client.get("/api/status", headers={"x-app-password": "wrong"}).status_code, 401)
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["runtime"], "Python / FastAPI")
        self.assertEqual(response.headers["cache-control"], "no-store")
        for secret in ("test-x-token", "test-whatsapp-token", "test-whatsapp-secret"):
            self.assertNotIn(secret, response.text)
        platforms = {p["platform"]: p["connected"] for p in response.json()["platforms"]}
        self.assertTrue(platforms["x"])
        self.assertFalse(platforms["youtube"])
        self.assertFalse(platforms["tiktok"])

    def test_cross_origin_writes_are_rejected_before_mutation(self):
        response = self.client.post(
            "/api/clients", headers={"origin": "https://other.example"},
            json={"name": "Unwanted client", "niche": "Tech"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.client.get("/api/clients").json()), 1)
        response = self.client.post(
            "/api/clients", headers={"origin": "http://testserver"},
            json={"name": "Allowed client", "niche": "Football"},
        )
        self.assertEqual(response.status_code, 201)

    def test_python_dashboard_renders_without_leaking_private_client_names(self):
        name = '<script>alert("client")</script>'
        response = self.client.post("/api/clients", json={"name": name, "niche": "Motivation"})
        self.assertEqual(response.status_code, 201)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Big ideas. Better content.", page.text)
        self.assertNotIn(name, page.text)
        self.assertNotIn("&lt;script&gt;", page.text)
        self.assertNotIn(response.json()["id"], page.text)
        for platform in ("facebook", "x", "youtube", "tiktok"):
            self.assertIn(f'id="count-{platform}"', page.text)
        self.assertNotIn("{%", page.text)
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual(self.client.get("/app.js").status_code, 200)
        self.assertEqual(self.client.get("/style.css").status_code, 200)

    def test_client_drafts_remain_separate_through_edit_and_delete(self):
        response = self.client.post("/api/clients", json={"name": "Client A", "niche": "Football"})
        self.assertEqual(response.status_code, 201)
        client_id = response.json()["id"]
        post = self.create_post(client_id=client_id)
        self.assertEqual(self.client.get("/api/posts").json(), [])
        self.assertEqual(len(self.client.get("/api/posts", params={"client": client_id}).json()), 1)
        edit = self.client.patch(f"/api/posts/{post['id']}", json={"platform": "x", "content": "Updated client draft"})
        self.assertEqual(edit.status_code, 200)
        self.assertEqual(edit.json()["client_id"], client_id)
        self.assertEqual(edit.json()["content"], "Updated client draft")
        publish = self.client.post(f"/api/posts/{post['id']}/publish")
        self.assertEqual(publish.status_code, 400)
        self.assertIn("per-client", publish.json()["error"])
        self.assertEqual(self.client.delete(f"/api/posts/{post['id']}").json(), {"deleted": True})
        self.assertEqual(self.client.get("/api/posts", params={"client": client_id}).json(), [])
        self.assertEqual(self.client.delete(f"/api/posts/{post['id']}").status_code, 404)
        self.assertEqual(self.requests, [])

    def test_template_generation_is_explicit_and_includes_video_scripts(self):
        response = self.client.post("/api/generate", json={"topic": "Practical AI tools", "platforms": ["x", "youtube", "tiktok"]})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["source"], "template")
        self.assertEqual({post["platform"] for post in result["posts"]}, {"x", "youtube", "tiktok"})
        for post in result["posts"]:
            self.assertIn("Practical AI tools", post["content"])
            if post["platform"] in ("youtube", "tiktok"):
                self.assertIn("Replace these prompts", post["content"])
        self.assertEqual(self.requests, [])

    def test_invalid_drafts_and_schedules_are_rejected(self):
        bad_posts = [
            {"platform": "x", "content": "x" * 281},
            {"platform": "x", "content": "   "},
            {"platform": "unknown", "content": "A draft"},
            {"platform": "x", "content": "A draft", "title": "t" * 151},
            {"platform": "x", "content": "A draft", "client_id": "missing-client"},
            {"platform": "x", "content": "A draft", "scheduled_at": "not-a-date"},
            {"platform": "x", "content": "A draft", "scheduled_at": "2030-01-01T09:00:00"},
            {"platform": "x", "content": "A draft", "scheduled_at": iso(utcnow() - timedelta(minutes=1))},
            {"platform": "facebook", "content": "A draft", "scheduled_at": self.future()},
            {"platform": "youtube", "content": "Video draft", "scheduled_at": self.future()},
        ]
        for body in bad_posts:
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/posts", json=body).status_code, 400)
        for platforms in (["unknown"], ["x", "x"], []):
            with self.subTest(platforms=platforms):
                self.assertEqual(self.client.post("/api/generate", json={"topic": "Tech", "platforms": platforms}).status_code, 400)
        self.assertEqual(self.client.get("/api/posts").json(), [])
        self.assertEqual(self.requests, [])

    def test_json_body_validation(self):
        for raw in (b"{broken", b"[]", b"x" * 65537):
            with self.subTest(size=len(raw)):
                response = self.client.post("/api/posts", content=raw, headers={"content-type": "application/json"})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/posts").json(), [])

    def test_approved_x_post_publishes_once_using_official_adapter(self):
        post = self.create_post(scheduled_at=self.future())
        post_id = post["id"]
        self.assertEqual(post["status"], "pending_approval")
        self.assertEqual(self.client.post(f"/api/posts/{post_id}/publish").status_code, 400)
        self.assertEqual(self.requests, [])
        approved = self.approve(post)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "scheduled")
        response = self.client.post(f"/api/posts/{post_id}/publish")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "published")
        self.assertEqual(response.json()["remote_id"], "mock-published-post")
        self.assertTrue(response.json()["published_at"])
        self.assertEqual(len(self.requests), 1)
        outgoing = self.requests[0]
        self.assertEqual(outgoing.method, "POST")
        self.assertEqual(str(outgoing.url), "https://api.x.com/2/tweets")
        self.assertEqual(json.loads(outgoing.content), {"text": post["content"]})
        self.assertEqual(outgoing.headers["authorization"], "Bearer test-x-token")
        self.assertEqual(self.client.post(f"/api/posts/{post_id}/publish").status_code, 400)
        self.assertEqual(len(self.requests), 1)
        edit = self.client.patch(f"/api/posts/{post_id}", json={"platform": "x", "content": "Altered published post"})
        self.assertEqual(edit.status_code, 400)

    def test_editing_revokes_approval_and_requires_another_decision(self):
        post = self.create_post(scheduled_at=self.future())
        path = f"/api/posts/{post['id']}"
        self.assertEqual(self.approve(post).status_code, 200)
        edit = self.client.patch(path, json={"platform": "x", "content": "Reviewed replacement", "scheduled_at": self.future()})
        self.assertEqual(edit.status_code, 200)
        self.assertEqual(edit.json()["status"], "pending_approval")
        self.assertIsNone(edit.json()["approved_at"])
        self.assertGreater(edit.json()["revision"], post["revision"])
        self.assertEqual(self.client.post(path + "/publish").status_code, 400)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.approve(edit.json()).status_code, 200)
        self.assertEqual(self.client.post(path + "/publish").status_code, 200)
        self.assertEqual(json.loads(self.requests[0].content), {"text": "Reviewed replacement"})

    def test_dashboard_approval_requires_current_revision(self):
        post = self.create_post(scheduled_at=self.future())
        path = f"/api/posts/{post['id']}"
        self.assertEqual(self.client.post(path + "/approve").status_code, 400)
        edited = self.client.patch(path, json={"platform": "x", "content": "Changed before dashboard approval", "scheduled_at": self.future()})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(self.approve(post).status_code, 400)
        self.assertEqual(self.saved_post(post["id"])["status"], "pending_approval")
        stale_reject = self.client.post(path + "/reject", json={"revision": post["revision"]})
        self.assertEqual(stale_reject.status_code, 400)
        self.assertEqual(self.saved_post(post["id"])["status"], "pending_approval")
        self.assertEqual(self.approve(edited.json()).status_code, 200)

    def test_whatsapp_verification_challenge(self):
        params = {"hub.mode": "subscribe", "hub.verify_token": self.settings["WHATSAPP_VERIFY_TOKEN"], "hub.challenge": "challenge-text"}
        response = self.client.get("/webhooks/whatsapp", params=params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "challenge-text")
        params["hub.verify_token"] = "wrong"
        self.assertEqual(self.client.get("/webhooks/whatsapp", params=params).status_code, 403)

    def test_signed_whatsapp_approval_and_replay(self):
        post = self.create_post(scheduled_at=self.future())
        self.assertGreaterEqual(post["revision"], 1)
        response = self.callback(post)
        self.assertEqual(response.status_code, 200)
        approved = self.saved_post(post["id"])
        self.assertEqual(approved["status"], "scheduled")
        self.assertTrue(approved["approved_at"])
        self.assertEqual(self.callback(post).status_code, 200)
        self.assertEqual(self.saved_post(post["id"]), approved)
        states = self.application.state.store.query("SELECT * FROM automation_state WHERE key='wa:message-one'")
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["value"], "processed")
        self.assertEqual(self.requests, [])

    def test_stale_whatsapp_button_cannot_approve_edited_content(self):
        post = self.create_post(scheduled_at=self.future())
        path = f"/api/posts/{post['id']}"
        edited = self.client.patch(path, json={"platform": "x", "content": "Changed after notification", "scheduled_at": self.future()})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(self.callback(post, message_id="old-button").status_code, 200)
        current = self.saved_post(post["id"])
        self.assertEqual(current["status"], "pending_approval")
        self.assertIsNone(current["approved_at"])
        self.assertIsNone(current["error"])
        self.assertEqual(current["content"], "Changed after notification")
        self.assertEqual(self.callback(edited.json(), message_id="latest-button").status_code, 200)
        self.assertEqual(self.saved_post(post["id"])["status"], "scheduled")

    def test_whatsapp_signature_sender_and_business_phone_checks(self):
        post = self.create_post(scheduled_at=self.future())
        self.assertEqual(self.callback(post, signature=False).status_code, 403)
        self.assertEqual(self.callback(post, sender="untrusted-recipient").status_code, 200)
        self.assertEqual(self.callback(post, phone="wrong-business-phone").status_code, 200)
        current = self.saved_post(post["id"])
        self.assertEqual(current["status"], "pending_approval")
        self.assertIsNone(current["approved_at"])
        self.assertEqual(self.application.state.store.query("SELECT * FROM automation_state"), [])
        self.assertEqual(self.callback(post).status_code, 200)
        self.assertEqual(self.saved_post(post["id"])["status"], "scheduled")


if __name__ == "__main__":
    unittest.main()
