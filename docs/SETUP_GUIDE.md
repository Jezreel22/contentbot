# Connect Contentbot to WhatsApp and publishing

This guide prepares the current Python application for a personal pilot: WhatsApp approval requests, Facebook Page/X text publishing, and daily plans. Other client workspaces currently support drafts and exports; per-client OAuth and YouTube/TikTok uploads require further development. Use **My brand** for the first live test.

The implementation has been checked locally. Live documentation could not be fetched from the development workspace, so Meta/hosting menu names may differ. Follow the official documentation linked below when a screen differs.

## 1. Prepare accounts

Have access to GitHub, a Meta developer account, a Meta business portfolio/WhatsApp Business setup, a Facebook Page you manage, and an always-on Python host. Start with Meta's test WhatsApp sender. Your personal WhatsApp number receives approvals; the business sender and its phone-number ID are separate.

OpenAI is optional for original AI drafts. X is optional for the first Facebook publishing test. Hosting, WhatsApp, X, and OpenAI may have charges; review their current plans. OpenAI API billing is separate from a ChatGPT subscription.

## 2. Save the app to GitHub

The source is currently in `/workspace/contentbot`; a Git-backed hosting service needs it committed and pushed. Before committing, ensure credentials remain in secure environment settings and `.env`, `.data`, `.venv`, and logs are excluded by `.gitignore`.

```sh
cd /workspace/contentbot
git status --short
git add .gitignore .env.example README.md app.py automation.py core.py providers.py store.py requirements.txt requirements.lock public templates tests docs
git diff --cached --stat
git commit -m "Build Python Contentbot with approval workflows"
git push -u origin HEAD:main
```

If Git requests your identity, configure your own commit name/email locally. Check the `main` branch on GitHub contains `app.py` and `requirements.lock`. Do not use force-push or include credentials.

Review/save the cloud configuration and publish the development environment to retain that setup. This is separate from deploying a continuously running public web service.

## 3. Deploy an always-on service

Render is one example. Use a Python **Web Service**, connect `Jezreel22/contentbot`, and deploy branch `main`. Select Python 3.12+, a service plan that stays awake, one instance/replica, and these commands:

Build:

```sh
pip install --no-cache-dir -r requirements.lock
```

Start:

```sh
python -m uvicorn app:app --host 0.0.0.0 --port "$PORT" --workers 1 --no-access-log
```

The scheduler runs in the server process; multiple replicas/workers can duplicate scheduling work. The host supplies `PORT`. Use `/` as the hosting health-check path, since API routes require the dashboard password.

Attach a persistent disk, for example at `/var/data`, and set `DATA_DIR=/var/data/contentbot`. SQLite creates `contentbot.db` there. Without durable storage, redeployment may erase client drafts and plans. A new deployment starts with a fresh database unless you deliberately transfer a secure database backup.

Set `APP_PASSWORD` to a strong unique password in the host's environment settings. Save and redeploy/restart. Open the HTTPS address the host assigns and enter that password when prompted. This is a single-operator pilot; client logins, roles, and per-client publishing still need implementation before offering a multi-user service.

## 4. Create the Meta/WhatsApp app

At Meta for Developers, register your developer account if needed, create an app/use case that supports WhatsApp Business, and connect your business portfolio. Follow Meta's Cloud API getting-started flow. Menu labels can include WhatsApp, API Setup, Getting Started, and Configuration.

Start with the supplied test sender number. Add your personal number as an allowed test recipient and verify it when Meta asks. Send Meta's provided test message to confirm basic delivery. Keep the test phone-number ID, WhatsApp Business Account ID, and access token in secure settings. The Account ID and Phone Number ID are different.

For a Nigerian recipient, convert a local number starting with `0` to international digits by replacing that first `0` with `234`; no spaces. Keep your existing personal WhatsApp number as the recipient for this initial test.

## 5. Configure WhatsApp values on the host

| Variable | Where it comes from |
| --- | --- |
| `WHATSAPP_ACCESS_TOKEN` | Meta Cloud API access token authorized to send messages from your business account |
| `WHATSAPP_PHONE_NUMBER_ID` | Sender phone-number ID shown in API Setup; not the actual telephone number or WABA ID |
| `WHATSAPP_RECIPIENT` | Your verified recipient number in international digits |
| `WHATSAPP_APP_SECRET` | App Settings / Basic for the same Meta app that sends the webhook |
| `WHATSAPP_VERIFY_TOKEN` | A long random secret string you choose; use the identical string in Meta webhook configuration |
| `WHATSAPP_TEMPLATE_NAME` | Exact approved notification template name, for example `contentbot_post_approval` |
| `WHATSAPP_TEMPLATE_LANGUAGE` | Exact approved language code, for example `en_US` |

Use the host's environment/secrets interface. The application does not automatically load a `.env` file. Do not put these values in source control or chat. The app secret must be the real runtime secret because Python uses it locally to verify webhook signatures; a proxy placeholder does not work for that local calculation.

Temporary testing tokens expire. Before relying on unattended use, configure Meta's supported production/system-user token flow, assign the required app and WhatsApp Business assets, obtain messaging permissions, and manage expiry/revocation. The app does not refresh OAuth tokens automatically.

Before enabling notifications/live tokens, review existing approval requests and queued posts. Remove obsolete items; existing due approved posts can run when valid credentials become available.

## 6. Create the approval template

In WhatsApp Manager's message-template section, create `contentbot_post_approval`, use the intended language, and select the category permitted by Meta for this notification. Meta decides approval and category eligibility.

The body must contain exactly three text variables in this order:

```text
Contentbot approval request.
Client: {{1}}
Platform: {{2}}
Draft: {{3}}
Please choose Approve or Reject below.
```

Supply sample values such as `My brand`, `facebook`, and a harmless example draft. Add two **quick reply** buttons, in this order: **Approve**, then **Reject**. Do not choose URL/call buttons. The application supplies the per-post button payloads automatically.

Submit for approval and wait for Meta's approved status. The configured name/language must match exactly. The preview can be truncated to 700 characters, so review longer posts fully in the dashboard. The recipient must consent to these notifications.

A test account may restrict custom templates; if this setup cannot create/approve the template, complete the business sender setup Meta requires. A successful Meta default test message alone does not establish that this custom approval template works.

## 7. Register the webhook

Take the host's actual public HTTPS address and add `/webhooks/whatsapp`. Example placeholder:

```text
https://YOUR-SERVICE.onrender.com/webhooks/whatsapp
```

In the Meta app's WhatsApp webhook/configuration screen, enter that callback URL and the exact `WHATSAPP_VERIFY_TOKEN` saved on the host. Choose Verify and Save. Subscribe to the **messages** field and ensure the app is subscribed to the intended WhatsApp Business Account. Consult Meta's WABA subscription instructions if delivery is not enabled automatically by its setup flow.

Python handles the challenge and signed approval messages. A reverse proxy must preserve the POST body and `X-Hub-Signature-256` header. Keep query tokens out of proxy/request logs. The webhook is intentionally reachable without the dashboard password; it validates Meta's signature and recipient instead.

## 8. Connect a Facebook Page

Use a Meta app with the applicable Facebook/Pages API use case and permissions. Depending on Meta's app setup, this can require a separate app from the WhatsApp app. You must have permission to manage the Page.

For an initial test, use Meta's Graph API Explorer with your chosen app. Obtain a user access token with `pages_show_list`, `pages_read_engagement`, and `pages_manage_posts` as required. Authorize the intended Page, then use the Explorer's Page-token option or request:

```text
GET /me/accounts?fields=id,name,access_token
```

Choose the correct Page. Copy its Page ID to `FACEBOOK_PAGE_ID` and its Page access token to `FACEBOOK_PAGE_ACCESS_TOKEN` in the host's secure settings. Treat Explorer responses as secret material; do not paste them into chat. Save and restart the service.

Test tokens are unsuitable for unattended production. Use Meta's supported long-lived Page-token/business authorization process and complete any permissions review required for the accounts you manage. The current app supports Page text posts, not personal-profile publishing or Facebook video uploads.

## 9. Optionally connect X

Register an app/project in X's developer portal and enable user-authenticated posting. The current adapter requires an **OAuth 2 user access token** with `tweet.write` and the other scopes X requires. An application-only bearer token or OAuth 1 access-token/secret pair does not match this adapter.

Obtain the user token through X's documented OAuth 2 Authorization Code with PKCE consent flow. This app has no OAuth connection screen yet. You can use an OAuth-capable API client such as Postman or add an OAuth helper before onboarding X. Register that client's exact callback URL in X, authorize your own account, and securely save the resulting user token as `X_ACCESS_TOKEN`.

Token expiry/refresh must be managed: automated refresh is not implemented. First complete the Facebook/WhatsApp test; X can be added afterward. Review X's current API plan and posting access.

## 10. Optionally enable original AI drafts

Create an OpenAI API project/key, configure API billing, and save `OPENAI_API_KEY` securely on the host. Keep `AI_MODEL=gpt-4.1-mini` if your account supports it, or choose a compatible model with JSON chat-completions support. Restart the service.

Without a key, generation returns labeled templates containing prompts to complete. Review facts, match scores, and the final wording before approving any draft. A ChatGPT subscription does not supply this app's API key/billing.

## 11. Test rejection before live publishing

Open **My brand**, then **Daily posting plan**. Use `Africa/Lagos`, Facebook daily maximum `1`, all other platforms `0`, and keep automatic daily preparation off. Save the plan.

Create a harmless Facebook draft and schedule it at least 10 minutes in the future. The date/time input follows your browser's local timezone; the daily limit follows the plan timezone. Saving a scheduled draft submits it for approval.

Within roughly 15 seconds, expect a WhatsApp notification. Press Reject. In the dashboard, verify the post becomes `rejected`. This confirms the return webhook without posting publicly.

Create a fresh approval request from that draft or a new test draft, with a future schedule. Verify the content in the current notification, then press Approve when you intend to publish. The queue should show `scheduled`. Close the browser if you want to test unattended operation. Keep the host running.

After the scheduled time plus a worker interval, verify the actual post on the Page and `published` status in Contentbot. The app does not currently send a WhatsApp approval acknowledgment or publication-success message; use the dashboard/Page to check results.

If a publishing request times out, inspect the Page before reapproving: the platform may have accepted it even if the bot did not receive the response.

## 12. Enable daily automation

Set per-platform daily maximums from 0 to 10, topics (one per line), timezone, and a posting window. Start conservatively, for example Facebook 1 and X 2 once both work. Keep YouTube/TikTok at 0 for this live workflow.

Enable **Automatically prepare daily drafts**, save, and choose **Prepare today’s approval drafts** to create the first batch. This works only when slots remain within today's window; after the end hour, wait for the next day or change the window. The worker checks preparation hourly and notifications/publication about every 15 seconds.

Approve each draft from its latest notification. Editing invalidates old approval buttons. Unapproved/rejected posts do not publish. Counts are maximums, not guaranteed output; missing approvals or credentials can reduce output. Late approval moves a past schedule to at least one minute ahead, with quota checks.

Turning off automatic preparation stops new drafts, not approved queued posts. To stop publishing, set the platform count to 0 or remove its queued posts. A lower cap can leave approved posts waiting for a later day's quota.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Host redeploy loses drafts | Persistent disk and `DATA_DIR`; database backups |
| Webhook verification fails | Public HTTPS endpoint, running app, exact verify-token match, proxy routing |
| WhatsApp approval message missing | Recipient verified/opted in, token, sender ID, approved template/name/language, host logs |
| Message arrives but button has no effect | `messages` subscription, WABA/app subscription, same-app app secret, sender metadata, latest draft revision |
| Dashboard says connected but publish fails | Indicators check variable presence only; validate actual token, permissions, Page ID, and API plan |
| No daily drafts | Plan active, nonzero counts, remaining slots, generation errors, worker running |
| Approved post remains queued | Schedule, timezone, daily cap, server running |
| Draft still pending after approval | Connection/permission/quota error in inbox; unsupported client or platform |

Keep the host awake, back up SQLite securely, monitor token expiry and failures, and complete production authentication/roles/per-client OAuth before using this as a client-facing service.

Official documentation:

- https://developers.facebook.com/docs/whatsapp/cloud-api/get-started
- https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components
- https://developers.facebook.com/docs/whatsapp/cloud-api/guides/set-up-webhooks
- https://developers.facebook.com/docs/pages-api/posts
- https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code
- https://render.com/docs/deploy-fastapi
- https://render.com/docs/disks
- https://platform.openai.com/api-keys
