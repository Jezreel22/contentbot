"""Daily planning, approval notifications, and the single-process posting worker."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import math
import re
from datetime import datetime, timedelta
from uuid import uuid4
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import iso, parse_time, utcnow

logger = logging.getLogger(__name__)
PLATFORMS = ("facebook", "x", "youtube", "tiktok")
DEFAULT_PLAN = {
    "active": False,
    "timezone": "Africa/Lagos",
    "startHour": 9,
    "endHour": 20,
    "counts": {"facebook": 1, "x": 2, "youtube": 0, "tiktok": 0},
    "topics": ["Tech", "Football", "Motivation"],
}
CALLBACK = re.compile(r"^(approve|reject):([a-f0-9-]{36}):([1-9][0-9]{0,9})$")


class Automation:
    def __init__(self, store, providers, publisher):
        self.store = store
        self.providers = providers
        self.publisher = publisher
        self._lock = asyncio.Lock()
        self._last_fill = None
        store.execute(
            "CREATE TABLE IF NOT EXISTS plans "
            "(client_id TEXT PRIMARY KEY, config TEXT NOT NULL)"
        )
        store.execute(
            "CREATE TABLE IF NOT EXISTS notifications "
            "(id TEXT PRIMARY KEY, post_id TEXT NOT NULL, client_id TEXT NOT NULL, "
            "message TEXT NOT NULL, created_at TEXT NOT NULL, "
            "sent INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1)"
        )
        store.execute(
            "CREATE TABLE IF NOT EXISTS automation_state "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        columns = {row["name"] for row in store.query("PRAGMA table_info(posts)")}
        for name, definition in (
            ("approved_at", "TEXT"),
            ("plan_slot", "TEXT"),
            ("revision", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if name not in columns:
                store.execute(f"ALTER TABLE posts ADD COLUMN {name} {definition}")
        invalid_revisions = store.query("SELECT id FROM posts WHERE revision IS NULL OR revision<1")
        store.execute("UPDATE posts SET revision=1 WHERE revision IS NULL OR revision<1")
        notification_columns = {
            row["name"] for row in store.query("PRAGMA table_info(notifications)")
        }
        legacy_notifications = "revision" not in notification_columns
        if legacy_notifications:
            store.execute(
                "ALTER TABLE notifications ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )
        store.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS unique_plan_slot "
            "ON posts(client_id, platform, plan_slot) WHERE plan_slot IS NOT NULL"
        )
        # Old schedules must receive an explicit approval before becoming eligible.
        unapproved_schedules = store.query(
            "SELECT id FROM posts WHERE status='scheduled' AND approved_at IS NULL"
        )
        store.execute(
            "UPDATE posts SET status='pending_approval' "
            "WHERE status='scheduled' AND approved_at IS NULL"
        )
        # Sent Node-era buttons did not carry a revision. Replace them once with
        # a preview and buttons for the current content rather than leaving
        # existing pending posts with unusable approval notifications.
        if legacy_notifications:
            store.execute("UPDATE notifications SET sent=1")
            for post in store.query("SELECT * FROM posts WHERE status='pending_approval'"):
                self.notify(post)
        else:
            for post_id in {row["id"] for row in invalid_revisions + unapproved_schedules}:
                post = store.get(post_id)
                if post["status"] == "pending_approval":
                    store.execute("UPDATE notifications SET sent=1 WHERE post_id=?", (post["id"],))
                    self.notify(post)

    def read_plan(self, client):
        row = self.store.one("SELECT config FROM plans WHERE client_id=?", (client,))
        return json.loads(row["config"]) if row else copy.deepcopy(DEFAULT_PLAN)

    def save_plan(self, client, body):
        if not self.store.one("SELECT id FROM clients WHERE id=?", (client,)):
            raise ValueError("Client not found")
        if not isinstance(body, dict):
            raise ValueError("Choose a valid posting plan")
        timezone = body.get("timezone")
        try:
            if not isinstance(timezone, str):
                raise ValueError
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Choose a valid IANA timezone") from None
        start, end = body.get("startHour"), body.get("endHour")
        if (
            not isinstance(body.get("active"), bool)
            or type(start) is not int
            or type(end) is not int
            or start < 0
            or end > 23
            or end < start
        ):
            raise ValueError("Choose a valid posting window")
        counts = body.get("counts")
        if (
            not isinstance(counts, dict)
            or set(counts) != set(PLATFORMS)
            or any(type(counts[p]) is not int or not 0 <= counts[p] <= 10 for p in PLATFORMS)
        ):
            raise ValueError("Set each platform to 0–10 posts per day")
        topics = body.get("topics")
        if (
            not isinstance(topics, list)
            or not 1 <= len(topics) <= 20
            or any(not isinstance(t, str) or not t.strip() or len(t) > 200 for t in topics)
        ):
            raise ValueError("Add 1–20 topics (up to 200 characters each)")
        plan = {
            "active": body["active"],
            "timezone": timezone,
            "startHour": start,
            "endHour": end,
            "counts": {p: counts[p] for p in PLATFORMS},
            "topics": [t.strip() for t in topics],
        }
        self.store.execute(
            "INSERT INTO plans(client_id, config) VALUES(?,?) "
            "ON CONFLICT(client_id) DO UPDATE SET config=excluded.config",
            (client, json.dumps(plan)),
        )
        return plan

    def notify(self, post):
        self.store.execute(
            "INSERT INTO notifications(id,post_id,client_id,message,created_at,sent,revision) "
            "VALUES(?,?,?,?,?,0,?)",
            (
                str(uuid4()),
                post["id"],
                post["client_id"],
                f"{post['platform']}: {post.get('title') or 'Post'} needs your approval",
                iso(utcnow()),
                post["revision"],
            ),
        )

    def submit(self, post_id):
        post = self.store.get(post_id)
        if not post or post["status"] not in ("draft", "failed", "rejected"):
            raise ValueError("This post cannot be submitted for approval")
        # New approval requests invalidate buttons issued for an earlier request.
        self.store.execute(
            "UPDATE posts SET status='pending_approval',approved_at=NULL,error=NULL,"
            "revision=revision+1 WHERE id=?",
            (post_id,),
        )
        post = self.store.get(post_id)
        self.notify(post)
        return post

    def decide(self, post_id, decision, revision=None):
        post = self.store.get(post_id)
        if not post or post["status"] != "pending_approval":
            raise ValueError("This approval is no longer pending")
        if revision is not None and revision != post["revision"]:
            raise ValueError("This post was changed. Use the latest approval notification.")
        if decision == "reject":
            self.store.execute(
                "UPDATE posts SET status='rejected',approved_at=NULL WHERE id=?", (post_id,)
            )
            return self.store.get(post_id)
        if decision != "approve":
            raise ValueError("Invalid decision")
        if post["client_id"] != "default" or not self.providers.enabled(post["platform"]):
            raise ValueError(
                "Connect this client’s platform before approval. You can reject or export this draft."
            )
        plan = self.read_plan(post["client_id"])
        now = utcnow()
        at = parse_time(post["scheduled_at"]) if post["scheduled_at"] else now + timedelta(minutes=1)
        if at <= now:
            at = now + timedelta(minutes=1)
        date = self._local_date(at, plan["timezone"])
        existing = self.store.query(
            "SELECT scheduled_at,published_at FROM posts "
            "WHERE client_id=? AND platform=? AND id!=? "
            "AND status IN ('scheduled','publishing','published')",
            (post["client_id"], post["platform"], post_id),
        )
        count = sum(
            1
            for row in existing
            if self._same_date(row["published_at"] or row["scheduled_at"], date, plan["timezone"])
        )
        cap = plan["counts"][post["platform"]]
        if cap == 0 or count >= cap:
            raise ValueError(
                "Daily posting limit reached. Change the schedule or increase the daily limit."
            )
        self.store.execute(
            "UPDATE posts SET status='scheduled',approved_at=?,scheduled_at=?,error=NULL "
            "WHERE id=? AND status='pending_approval'",
            (iso(now), iso(at), post_id),
        )
        return self.store.get(post_id)

    @staticmethod
    def _local_date(moment, timezone):
        return moment.astimezone(ZoneInfo(timezone)).date().isoformat()

    @classmethod
    def _same_date(cls, value, date, timezone):
        if not value:
            return False
        try:
            return cls._local_date(parse_time(value), timezone) == date
        except (ValueError, TypeError):
            return False

    def _slots(self, plan, now):
        timezone = ZoneInfo(plan["timezone"])
        today = now.astimezone(timezone).date()
        rounded = datetime.fromtimestamp(math.ceil(now.timestamp() / 300) * 300, now.tzinfo)
        result = []
        # Walk UTC so repeated and skipped daylight-saving hours remain valid.
        for offset in range(0, 48 * 60, 5):
            moment = rounded + timedelta(minutes=offset)
            local = moment.astimezone(timezone)
            if local.date() == today and plan["startHour"] * 60 <= local.hour * 60 + local.minute <= plan["endHour"] * 60:
                result.append(moment)
        return result

    async def fill(self, client):
        async with self._lock:
            return await self._fill(client)

    async def _fill(self, client):
        if not self.store.one("SELECT id FROM clients WHERE id=?", (client,)):
            raise ValueError("Client not found")
        plan = self.read_plan(client)
        if not plan["active"]:
            return {"created": 0}
        now = utcnow()
        date = self._local_date(now, plan["timezone"])
        available = self._slots(plan, now)
        if not available:
            return {"created": 0}
        created = 0
        for platform, count in plan["counts"].items():
            for index in range(count):
                slot = f"{date}:{index}"
                if self.store.one(
                    "SELECT id FROM posts WHERE client_id=? AND platform=? AND plan_slot=?",
                    (client, platform, slot),
                ):
                    continue
                topic = plan["topics"][(index + now.day) % len(plan["topics"])]
                result = await self.providers.generate(
                    {"topic": f"{topic}: original angle {index + 1} for {date}", "platforms": [platform]}
                )
                post = next((p for p in result["posts"] if p["platform"] == platform), None)
                if not post:
                    raise ValueError("Generator did not return the requested platform")
                at = available[min(len(available) - 1, int((index + 0.5) * len(available) / count))]
                post_id = str(uuid4())
                result = self.store.execute(
                    "INSERT OR IGNORE INTO posts"
                    "(id,platform,content,title,status,scheduled_at,created_at,client_id,plan_slot) "
                    "VALUES(?,?,?,?,'pending_approval',?,?,?,?)",
                    (post_id, platform, post["content"], post.get("title") or "", iso(at), iso(now), client, slot),
                )
                if result.rowcount:
                    self.notify(self.store.get(post_id))
                    created += 1
        return {"created": created}

    def can_publish(self, post):
        if not post.get("approved_at"):
            return False
        plan = self.read_plan(post["client_id"])
        today = self._local_date(utcnow(), plan["timezone"])
        published = self.store.query(
            "SELECT published_at FROM posts WHERE client_id=? AND platform=? AND status='published'",
            (post["client_id"], post["platform"]),
        )
        count = sum(self._same_date(row["published_at"], today, plan["timezone"]) for row in published)
        return count < plan["counts"][post["platform"]]

    def telegram_enabled(self):
        return bool(self.providers.env.get("TELEGRAM_BOT_TOKEN") and self.providers.env.get("TELEGRAM_CHAT_ID"))

    def whatsapp_enabled(self):
        return all(
            self.providers.env.get(name)
            for name in (
                "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_RECIPIENT",
                "WHATSAPP_TEMPLATE_NAME", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN",
            )
        )

    async def _telegram(self, method, payload):
        response = await self.providers.client.post(
            f"https://api.telegram.org/bot{self.providers.env['TELEGRAM_BOT_TOKEN']}/{method}",
            json=payload,
            timeout=10,
        )
        if not response.is_success:
            raise ValueError(f"Telegram HTTP {response.status_code}")
        result = response.json()
        if not result.get("ok"):
            raise ValueError("Telegram request failed")
        return result.get("result")

    def _pending_notifications(self):
        for notification in self.store.query(
            "SELECT * FROM notifications WHERE sent=0 ORDER BY created_at LIMIT 10"
        ):
            post = self.store.get(notification["post_id"])
            if not post or post["status"] != "pending_approval" or post["revision"] != notification["revision"]:
                self.store.execute("UPDATE notifications SET sent=1 WHERE id=?", (notification["id"],))
                continue
            yield notification, post

    @staticmethod
    def _action(post, decision):
        return f"{decision}:{post['id']}:{post['revision']}"

    async def _deliver_whatsapp(self):
        if not self.whatsapp_enabled():
            return
        env = self.providers.env
        for notification, post in self._pending_notifications():
            client = self.store.one("SELECT name FROM clients WHERE id=?", (post["client_id"],))
            response = await self.providers.client.post(
                f"https://graph.facebook.com/v22.0/{quote(env['WHATSAPP_PHONE_NUMBER_ID'], safe='')}/messages",
                headers={"Authorization": f"Bearer {env['WHATSAPP_ACCESS_TOKEN']}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": env["WHATSAPP_RECIPIENT"].lstrip("+"),
                    "type": "template",
                    "template": {
                        "name": env["WHATSAPP_TEMPLATE_NAME"],
                        "language": {"code": env.get("WHATSAPP_TEMPLATE_LANGUAGE") or "en_US"},
                        "components": [
                            {"type": "body", "parameters": [
                                {"type": "text", "text": client["name"]},
                                {"type": "text", "text": post["platform"]},
                                {"type": "text", "text": re.sub(r"\s+", " ", post["content"])[:700]},
                            ]},
                            {"type": "button", "sub_type": "quick_reply", "index": "0", "parameters": [
                                {"type": "payload", "payload": self._action(post, "approve")},
                            ]},
                            {"type": "button", "sub_type": "quick_reply", "index": "1", "parameters": [
                                {"type": "payload", "payload": self._action(post, "reject")},
                            ]},
                        ],
                    },
                },
                timeout=10,
            )
            if not response.is_success:
                raise ValueError("WhatsApp notification delivery failed")
            self.store.execute("UPDATE notifications SET sent=1 WHERE id=?", (notification["id"],))

    async def whatsapp_webhook(self, raw_bytes, signature):
        env = self.providers.env
        secret = env.get("WHATSAPP_APP_SECRET")
        if not secret or len(raw_bytes) > 262144:
            raise ValueError("Invalid WhatsApp webhook signature")
        expected = "sha256=" + hmac.new(secret.encode(), raw_bytes, hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature.encode(), expected.encode()):
            raise ValueError("Invalid WhatsApp webhook signature")
        try:
            payload = json.loads(raw_bytes)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Invalid WhatsApp webhook payload") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("entry", []), list):
            raise ValueError("Invalid WhatsApp webhook payload")
        for entry in payload.get("entry", []):
            if not isinstance(entry, dict) or not isinstance(entry.get("changes", []), list):
                continue
            for change in entry.get("changes", []):
                if not isinstance(change, dict) or not isinstance(change.get("value"), dict):
                    continue
                value = change["value"]
                metadata = value.get("metadata")
                if not isinstance(metadata, dict) or str(metadata.get("phone_number_id")) != str(env.get("WHATSAPP_PHONE_NUMBER_ID")):
                    continue
                messages = value.get("messages", [])
                if not isinstance(messages, list):
                    continue
                for message in messages:
                    if not isinstance(message, dict) or message.get("from") != (env.get("WHATSAPP_RECIPIENT") or "").lstrip("+"):
                        continue
                    button = message.get("button")
                    action = button.get("payload") if isinstance(button, dict) else None
                    interactive = message.get("interactive")
                    if not action and isinstance(interactive, dict):
                        reply = interactive.get("button_reply")
                        action = reply.get("id") if isinstance(reply, dict) else None
                    match = CALLBACK.fullmatch(action) if isinstance(action, str) else None
                    message_id = message.get("id")
                    if not match or not isinstance(message_id, str) or not message_id or len(message_id) > 512:
                        continue
                    key = f"wa:{message_id}"
                    if self.store.one("SELECT key FROM automation_state WHERE key=?", (key,)):
                        continue
                    decision, post_id, revision = match.groups()
                    state = "processed"
                    try:
                        self.decide(post_id, decision, int(revision))
                    except ValueError as error:
                        state = "ignored"
                        # A stale button must not overwrite the latest draft's error state.
                        self.store.execute(
                            "UPDATE posts SET error=? WHERE id=? AND status='pending_approval' AND revision=?",
                            (str(error), post_id, int(revision)),
                        )
                    self.store.execute(
                        "INSERT OR IGNORE INTO automation_state(key,value) VALUES(?,?)", (key, state)
                    )

    async def _deliver_telegram(self):
        if not self.telegram_enabled():
            return
        for notification, post in self._pending_notifications():
            client = self.store.one("SELECT name FROM clients WHERE id=?", (post["client_id"],))
            await self._telegram(
                "sendMessage",
                {
                    "chat_id": self.providers.env["TELEGRAM_CHAT_ID"],
                    "text": (
                        f"Approval requested · {client['name']}\n"
                        f"{post['platform'].upper()} · {post['scheduled_at'] or 'Next available time'}\n\n"
                        f"{post['content'][:3200]}"
                    ),
                    "reply_markup": {"inline_keyboard": [[
                        {"text": "Approve", "callback_data": self._action(post, "approve")},
                        {"text": "Reject", "callback_data": self._action(post, "reject")},
                    ]]},
                },
            )
            self.store.execute("UPDATE notifications SET sent=1 WHERE id=?", (notification["id"],))

    async def _poll_telegram(self):
        if not self.telegram_enabled():
            return
        row = self.store.one("SELECT value FROM automation_state WHERE key='telegram_offset'")
        offset = int(row["value"]) if row else 0
        updates = await self._telegram(
            "getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["callback_query"]}
        )
        for update in updates or []:
            query = update.get("callback_query")
            if (
                query
                and str(query.get("message", {}).get("chat", {}).get("id")) == str(self.providers.env["TELEGRAM_CHAT_ID"])
                and (
                    not self.providers.env.get("TELEGRAM_APPROVER_ID")
                    or str(query.get("from", {}).get("id")) == self.providers.env["TELEGRAM_APPROVER_ID"]
                )
            ):
                match = CALLBACK.fullmatch(query.get("data", ""))
                text = "This approval button is no longer valid. Use the latest notification."
                if match:
                    decision, post_id, revision = match.groups()
                    try:
                        self.decide(post_id, decision, int(revision))
                        text = "Approved and scheduled" if decision == "approve" else "Rejected"
                    except ValueError as error:
                        text = str(error)
                await self._telegram(
                    "answerCallbackQuery",
                    {"callback_query_id": query["id"], "text": text[:190], "show_alert": True},
                )
            self.store.execute(
                "INSERT INTO automation_state(key,value) VALUES('telegram_offset',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(update["update_id"] + 1),),
            )

    async def tick(self):
        if self._lock.locked():
            return
        async with self._lock:
            now = utcnow()
            if self._last_fill is None or now - self._last_fill > timedelta(hours=1):
                self._last_fill = now
                for row in self.store.query("SELECT client_id FROM plans"):
                    try:
                        await self._fill(row["client_id"])
                    except Exception:
                        logger.warning("Daily plan generation failed; retry from dashboard.")
            try:
                if self.whatsapp_enabled():
                    await self._deliver_whatsapp()
                else:
                    await self._deliver_telegram()
                    await self._poll_telegram()
            except Exception:
                logger.warning("Notification provider unavailable; pending approvals remain in the inbox.")
            for post in self.store.query(
                "SELECT * FROM posts WHERE status='scheduled' AND scheduled_at<=? ORDER BY scheduled_at",
                (iso(utcnow()),),
            ):
                if not self.can_publish(post):
                    continue
                try:
                    await self.publisher(post["id"])
                except Exception as error:
                    self.store.execute(
                        "UPDATE posts SET status='failed',error=? WHERE id=? AND status='scheduled'",
                        (str(error), post["id"]),
                    )
