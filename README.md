# Contentbot

A responsive content studio for tech, football, and motivation creators managing multiple client workspaces. Built with Python 3.12, FastAPI, Jinja2 templates, and SQLite. The dashboard is served by Python; the browser uses HTML, CSS, and JavaScript for its interactive controls.

For a full account-to-publishing walkthrough, see [the setup guide](docs/SETUP_GUIDE.md).

## Run

```sh
cd /workspace/contentbot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 3000 --no-access-log
```

Open `http://127.0.0.1:3000`. Use `--no-access-log` to keep the webhook verification token out of request logs. Use a single server worker: the embedded background scheduler runs inside the application process. Run `.venv/bin/python -m unittest discover -s tests -v` for tests. Data persists in `.data/contentbot.db`; the Python application preserves the existing database. Back up that directory securely.

## Included

- Client workspaces with separate draft libraries.
- Platform-specific content generation, editing, text export, and character validation.
- Optional OpenAI generation; labeled templates work without credentials.
- Facebook Page text publishing and X text publishing through official APIs.
- Per-client daily plans: 0–10 posts per platform, topic rotation, posting windows, and timezone settings.
- An approval inbox and WhatsApp Cloud API approve/reject notifications.
- A durable queue checked every 15 seconds; only explicitly approved posts publish.
- Daily maximums enforced against published posts, including delayed approvals.
- Explicit publishing confirmation, failure reporting, and protection against simultaneous duplicate attempts.

The scheduler must remain running. Missed scheduled posts are processed on restart. Interrupted publish attempts require checking the social account before retrying because a network timeout can occur after a platform accepts a post.

## Configuration

See `.env.example` for variable names. Supply variables through your host's secure environment settings or export them securely before startup. The application does not automatically load `.env` files. Do not put credentials into source files, saved setup instructions, or chat.

Set `OPENAI_API_KEY` for real AI generation and optionally `AI_MODEL`. Facebook requires `FACEBOOK_PAGE_ID` and a Page access token with the applicable Page publishing permissions. X requires a user OAuth access token with `tweet.write` and appropriate API access. Tokens expire; refresh OAuth is not implemented. Verify current platform permissions and API access before connecting.

**Multiple clients:** content libraries are separate, but this is a single-operator dashboard, not a multi-user SaaS. Only “My brand” can publish using server credentials. Other workspaces generate and export drafts. Per-client OAuth, roles, and secure credential storage are necessary before enabling client publishing.

**YouTube and TikTok:** video scripts and exports are supported. Media storage, video uploads, platform OAuth, and required platform reviews are not implemented. No mock publishing or fabricated engagement statistics are shown.

## Before production

Keep the default localhost binding during development. Before exposing externally, set a strong `APP_PASSWORD`, use HTTPS through a reverse proxy, and add production authentication, per-client authorization, rate limiting, and monitoring. This initial release does not include account sign-up, OAuth connection flows, video generation, engagement analytics, or a continuously hosted scheduler. Drafts should be checked for factual accuracy, rights, and brand suitability. No engagement or growth outcome is guaranteed.

## Cloud startup

Use the existing checkout; cloud tasks are already isolated and need no worktree. Install and start with the Python commands above, then check `/api/status` and load the dashboard. Live processes do not persist in a filesystem snapshot. Do not include credentials in setup scripts. The configured HTTP port can be changed with Uvicorn's `--port` flag; binding beyond localhost requires the production protections described above.

## Daily plans and approvals

Open **Daily posting plan** for each client. Choose posts per day, timezone, posting window, and topics; enable automatic preparation and save. Zero disables publishing for that platform. The worker prepares drafts hourly for remaining slots in the current day, without regenerating existing slots. Use **Prepare today’s approval drafts** for an immediate run. Changing counts can add slots but does not automatically remove existing drafts.

Open **Approval inbox** to approve or reject. Saving a scheduled post submits it for approval; editing removes previous approval. Approval schedules automatic publishing. Late approval moves the post to at least one minute in the future and still observes the daily cap. Posts blocked by a reduced daily limit remain queued until quota is available. Failed publishing requires review and fresh approval before retrying. AI generation requires a configured provider; template mode creates prompts requiring manual completion.

## WhatsApp approval notifications

You provide the account access: create or use your Meta developer app and WhatsApp Business account, obtain the required API credentials and permissions, and add their values to secure environment settings. Your recipient's phone number and name are not API credentials. The application provides the webhook endpoint; deployment provides its public HTTPS address. Register that address and your verification token in Meta's dashboard. You do not need to write a separate webhook server.

Start with Meta's [WhatsApp Cloud API setup guide](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started) and [webhook documentation](https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components). Facebook Page and X publishing require developer access for those services too; see the [X developer portal](https://developer.x.com/). Per-client account connections and YouTube/TikTok uploads remain future work, even when credentials are available.

Use the official Meta WhatsApp Cloud API. Configure the variables in `.env.example` securely: access token, business phone-number ID, recipient number (digits including country code), app secret, webhook verification token, template name, and optional language code. These are not entered through the dashboard.

Create and obtain Meta approval for a template with **three body text parameters** (client name, platform, content preview), followed by **two quick-reply buttons in order: Approve, Reject**. Example body: `{{1}} has a {{2}} post awaiting approval: {{3}}`. Use an appropriate template category under Meta's rules. The recipient must opt in to notifications. Longer posts are previewed; review the full draft in the dashboard when needed.

Host the application behind public HTTPS with a strong `APP_PASSWORD`. Configure Meta's webhook callback URL as `https://YOUR-DOMAIN/webhooks/whatsapp`, using the same verification token, and subscribe to messages. The endpoint verifies Meta's HMAC signature, business phone ID, and configured recipient before accepting a button decision. Repeated callbacks do not approve a post twice. Delivery failures retain pending notifications for retry; check server logs and the approval inbox. Delivery can be duplicated if a network failure occurs after Meta accepts a message. Approval buttons are valid only while the post is pending; edits invalidate earlier approval state.

The server must run continuously for notification delivery, daily generation, and publication. Closing the browser is fine; stopping the server is not. Deploy to a continuously running host with HTTPS, monitoring, backups, and production authentication before relying on unattended use. Live WhatsApp delivery and live social posting have not been validated without credentials. WhatsApp approval is limited to the single configured recipient; per-client recipient routing is not implemented.

Telegram is an optional alternative when WhatsApp is not configured: set its bot token and chat ID, preferably a private chat; set approver ID for additional restriction. Do not share group approval access unintentionally.
