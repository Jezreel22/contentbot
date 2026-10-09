"""Offline checks for planning, notification delivery and approval safeguards."""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from automation import Automation
from core import iso
from providers import Providers
from store import Store


NOW = datetime(2026, 10, 9, 9, tzinfo=timezone.utc)
PLAN = {
    "active": True,
    "timezone": "Africa/Lagos",
    "startHour": 0,
    "endHour": 23,
    "counts": {"facebook": 0, "x": 2, "youtube": 0, "tiktok": 0},
    "topics": ["Tech", "Football"],
}
WHATSAPP = {
    "WHATSAPP_ACCESS_TOKEN": "offline-test-token",
    "WHATSAPP_PHONE_NUMBER_ID": "123",
    "WHATSAPP_RECIPIENT": "234123",
    "WHATSAPP_TEMPLATE_NAME": "contentbot_approval",
    "WHATSAPP_APP_SECRET": "offline-test-secret",
    "WHATSAPP_VERIFY_TOKEN": "offline-test-verify",
}


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.requests = []

        def transport(request):
            self.requests.append(request)
            return httpx.Response(200, json={"messages": [{"id": "offline-message"}]})

        self.providers = Providers({"X_ACCESS_TOKEN": "offline-x-token"}, transport=httpx.MockTransport(transport))
        self.published = []

        async def publish(post_id):
            self.published.append(post_id)
            self.store.execute(
                "UPDATE posts SET status='published',published_at=? WHERE id=?", (iso(NOW), post_id)
            )

        self.automation = Automation(self.store, self.providers, publish)
        self.clock = patch("automation.utcnow", return_value=NOW)
        self.clock.start()

    async def asyncTearDown(self):
        self.clock.stop()
        await self.providers.close()
        self.store.close()

    def post(self, status="pending_approval", scheduled_at=None, client="default"):
        post_id = str(uuid4())
        self.store.execute(
            "INSERT INTO posts(id,platform,content,title,status,scheduled_at,created_at,client_id) "
            "VALUES(?,'x','Useful original idea','Tech tip',?,?,?,?)",
            (post_id, status, scheduled_at, iso(NOW), client),
        )
        return self.store.get(post_id)

    def webhook_payload(self, post, message_id="message1", sender="234123", phone="123", revision=None):
        return json.dumps({
            "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": phone},
                "messages": [{
                    "id": message_id, "from": sender,
                    "button": {"payload": f"approve:{post['id']}:{revision or post['revision']}"},
                }],
            }}]}],
        }).encode()

    async def webhook(self, raw, valid=True):
        signature = "sha256=" + hmac.new(WHATSAPP["WHATSAPP_APP_SECRET"].encode(), raw, hashlib.sha256).hexdigest()
        await self.automation.whatsapp_webhook(raw, signature if valid else "forged")

    async def test_plan_validation_and_daily_generation_are_idempotent(self):
        for changes, message in [
            ({"counts": {**PLAN["counts"], "x": 11}}, "0–10"),
            ({"counts": {**PLAN["counts"], "x": True}}, "0–10"),
            ({"startHour": True}, "posting window"),
            ({"timezone": "Unknown/Nowhere"}, "timezone"),
            ({"topics": []}, "topics"),
        ]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.automation.save_plan("default", {**PLAN, **changes})
        self.automation.save_plan("default", PLAN)
        self.assertEqual(await self.automation.fill("default"), {"created": 2})
        self.assertEqual(await self.automation.fill("default"), {"created": 0})
        posts = self.store.query("SELECT * FROM posts")
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(p["status"] == "pending_approval" and p["approved_at"] is None for p in posts))
        self.assertTrue(all(p["revision"] > 0 for p in posts))
        self.assertEqual(self.store.one("SELECT COUNT(*) AS total FROM notifications")["total"], 2)
        self.assertFalse(self.automation.can_publish(posts[0]))

    async def test_explicit_approval_rejection_and_due_posting(self):
        self.automation.save_plan("default", PLAN)
        await self.automation.fill("default")
        posts = self.store.query("SELECT * FROM posts ORDER BY scheduled_at")
        first = self.automation.decide(posts[0]["id"], "approve")
        self.assertEqual(first["status"], "scheduled")
        self.assertEqual(first["approved_at"], iso(NOW))
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.automation.decide(first["id"], "approve")
        rejected = self.automation.decide(posts[1]["id"], "reject")
        self.assertEqual(rejected["status"], "rejected")
        self.assertIsNone(rejected["approved_at"])
        self.store.execute("UPDATE posts SET scheduled_at=? WHERE id=?", (iso(NOW - timedelta(minutes=1)), first["id"]))
        await self.automation.tick()
        await self.automation.tick()
        self.assertEqual(self.published, [first["id"]])
        self.assertEqual(self.store.get(first["id"])["status"], "published")

    async def test_caps_use_local_day_and_include_scheduled_reservations(self):
        at_midnight = datetime(2026, 10, 9, 23, 30, tzinfo=timezone.utc)
        self.automation.save_plan("default", {**PLAN, "active": False, "counts": {**PLAN["counts"], "x": 1}})
        pending = self.post()
        already = self.post("published")
        # UTC dates differ; both times are October 10 in Lagos.
        self.store.execute("UPDATE posts SET published_at=? WHERE id=?", (iso(at_midnight), already["id"]))
        with patch("automation.utcnow", return_value=at_midnight + timedelta(hours=1)):
            with self.assertRaisesRegex(ValueError, "Daily posting limit"):
                self.automation.decide(pending["id"], "approve")
            self.assertFalse(self.automation.can_publish({**pending, "approved_at": iso(NOW)}))
        self.store.execute("DELETE FROM posts WHERE id=?", (already["id"],))
        first = self.automation.decide(pending["id"], "approve")
        second = self.post()
        with self.assertRaisesRegex(ValueError, "Daily posting limit"):
            self.automation.decide(second["id"], "approve")
        self.assertEqual(self.store.get(first["id"])["status"], "scheduled")

    async def test_unconnected_clients_cannot_approve_or_publish(self):
        self.store.execute("INSERT INTO clients VALUES('another','Another client','Football')")
        another = self.post(client="another")
        with self.assertRaisesRegex(ValueError, "Connect this client"):
            self.automation.decide(another["id"], "approve")
        self.assertEqual(self.automation.decide(another["id"], "reject")["status"], "rejected")

    async def test_webhook_signature_recipient_phone_and_replay(self):
        self.providers.env.update(WHATSAPP)
        post = self.post()
        with self.assertRaisesRegex(ValueError, "signature"):
            await self.webhook(self.webhook_payload(post), valid=False)
        await self.webhook(self.webhook_payload(post, sender="wrong"))
        await self.webhook(self.webhook_payload(post, phone="wrong"))
        self.assertEqual(self.store.get(post["id"])["status"], "pending_approval")
        await self.webhook(self.webhook_payload(post))
        self.assertEqual(self.store.get(post["id"])["status"], "scheduled")
        await self.webhook(self.webhook_payload(post))
        self.assertEqual(self.store.one("SELECT COUNT(*) AS total FROM automation_state WHERE key='wa:message1'")["total"], 1)

    async def test_stale_content_and_resubmitted_approval_buttons_are_invalidated(self):
        self.providers.env.update(WHATSAPP)
        post = self.post()
        self.store.execute("UPDATE posts SET content='New reviewed idea',revision=revision+1 WHERE id=?", (post["id"],))
        await self.webhook(self.webhook_payload(post, message_id="old-content"))
        latest = self.store.get(post["id"])
        self.assertEqual(latest["status"], "pending_approval")
        self.assertIsNone(latest["error"])
        self.automation.decide(latest["id"], "reject")
        resubmitted = self.automation.submit(latest["id"])
        self.assertGreater(resubmitted["revision"], latest["revision"])
        await self.webhook(self.webhook_payload(latest, message_id="old-request"))
        self.assertEqual(self.store.get(latest["id"])["status"], "pending_approval")
        await self.webhook(self.webhook_payload(resubmitted, message_id="latest"))
        self.assertEqual(self.store.get(latest["id"])["status"], "scheduled")

    async def test_notification_preview_matches_revision_and_stale_notification_is_skipped(self):
        self.providers.env.update(WHATSAPP)
        post = self.post()
        self.automation.notify(post)
        self.store.execute("UPDATE posts SET content='Latest original idea',revision=revision+1 WHERE id=?", (post["id"],))
        latest = self.store.get(post["id"])
        self.automation.notify(latest)
        await self.automation.tick()
        self.assertEqual(len(self.requests), 1)
        body = json.loads(self.requests[0].content)
        parts = body["template"]["components"]
        self.assertEqual(parts[0]["parameters"][2]["text"], latest["content"])
        self.assertEqual(parts[1]["parameters"][0]["payload"], f"approve:{post['id']}:{latest['revision']}")
        self.assertLessEqual(len(parts[1]["parameters"][0]["payload"].encode()), 64)
        self.assertEqual(self.store.one("SELECT COUNT(*) AS total FROM notifications WHERE sent=0")["total"], 0)

    async def test_notification_failure_retains_pending_inbox_and_allows_retry(self):
        self.providers.env.update(WHATSAPP)
        post = self.post()
        self.automation.notify(post)
        self.providers.client.post = self._failed_post
        with self.assertLogs("automation", level="WARNING"):
            await self.automation.tick()
        self.assertEqual(self.store.get(post["id"])["status"], "pending_approval")
        self.assertEqual(self.store.one("SELECT sent FROM notifications")["sent"], 0)
        self.assertEqual(self.published, [])

    async def _failed_post(self, *args, **kwargs):
        return httpx.Response(503, json={"error": "temporarily unavailable"})

    async def test_legacy_schema_and_pending_schedules_are_migrated_without_data_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.db"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE posts(id TEXT PRIMARY KEY,platform TEXT NOT NULL,content TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,scheduled_at TEXT,
                    created_at TEXT NOT NULL,published_at TEXT,remote_id TEXT,error TEXT,
                    client_id TEXT NOT NULL DEFAULT 'default',approved_at TEXT,plan_slot TEXT);
                CREATE TABLE notifications(id TEXT PRIMARY KEY,post_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0);
            """)
            post_id = str(uuid4())
            old.execute("INSERT INTO posts(id,platform,content,status,created_at) VALUES(?,'x','Keep this draft','scheduled',?)", (post_id, iso(NOW)))
            old.execute("INSERT INTO notifications VALUES('notification',?,'default','Keep message',?,1)", (post_id, iso(NOW)))
            old.commit()
            old.close()
            migrated = Store(path)
            try:
                Automation(migrated, self.providers, self.automation.publisher)
                self.assertEqual(migrated.get(post_id)["status"], "pending_approval")
                self.assertEqual(migrated.get(post_id)["content"], "Keep this draft")
                self.assertEqual(migrated.get(post_id)["revision"], 1)
                self.assertEqual(migrated.one("SELECT revision FROM notifications")["revision"], 1)
                pending_notices = migrated.query("SELECT * FROM notifications WHERE sent=0")
                self.assertEqual(len(pending_notices), 1)
                self.assertEqual(pending_notices[0]["post_id"], post_id)
                self.assertEqual(pending_notices[0]["revision"], 1)
                Automation(migrated, self.providers, self.automation.publisher)
                self.assertEqual(migrated.one("SELECT COUNT(*) AS total FROM notifications WHERE sent=0")["total"], 1)
            finally:
                migrated.close()

    async def test_revision_zero_pending_notices_are_regenerated_once(self):
        post = self.post()
        self.store.execute("UPDATE posts SET revision=0 WHERE id=?", (post["id"],))
        self.automation.notify(self.store.get(post["id"]))
        self.store.execute("UPDATE notifications SET sent=1 WHERE post_id=?", (post["id"],))
        migrated = Automation(self.store, self.providers, self.automation.publisher)
        self.assertEqual(self.store.get(post["id"])["revision"], 1)
        pending_notices = self.store.query("SELECT * FROM notifications WHERE sent=0")
        self.assertEqual(len(pending_notices), 1)
        self.assertEqual(pending_notices[0]["revision"], 1)
        Automation(self.store, self.providers, migrated.publisher)
        self.assertEqual(self.store.one("SELECT COUNT(*) AS total FROM notifications WHERE sent=0")["total"], 1)

    async def test_unapproved_legacy_schedules_receive_notification_when_none_existed(self):
        post = self.post("scheduled", iso(NOW + timedelta(minutes=5)))
        self.assertEqual(self.store.one("SELECT COUNT(*) AS total FROM notifications")["total"], 0)
        migrated = Automation(self.store, self.providers, self.automation.publisher)
        self.assertEqual(self.store.get(post["id"])["status"], "pending_approval")
        notice = self.store.one("SELECT * FROM notifications WHERE sent=0")
        self.assertEqual(notice["post_id"], post["id"])
        self.assertEqual(notice["revision"], self.store.get(post["id"])["revision"])
        Automation(self.store, self.providers, migrated.publisher)
        self.assertEqual(self.store.one("SELECT COUNT(*) AS total FROM notifications WHERE sent=0")["total"], 1)


if __name__ == "__main__":
    unittest.main()
