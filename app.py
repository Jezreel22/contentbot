"""Contentbot's Python dashboard, API, and single-process background worker."""
import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from core import iso, parse_time, utcnow
from store import Store
from providers import Providers, PLATFORMS, validate_post
from automation import Automation

ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))


async def read_body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            raise ValueError("Request too large")
    try:
        result = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid JSON") from None
    if not isinstance(result, dict):
        raise ValueError("Request must be a JSON object")
    return result


def create_app(data_dir=None, env=None, transport=None, run_worker=True):
    settings = dict(os.environ if env is None else env)

    @asynccontextmanager
    async def lifespan(application):
        store = Store(Path(data_dir or settings.get("DATA_DIR") or ROOT / ".data") / "contentbot.db")
        providers = Providers(settings, transport=transport)
        publish_lock = asyncio.Lock()
        automation = None

        async def publish(post_id):
            async with publish_lock:
                post = store.get(post_id)
                if not post:
                    raise ValueError("Post not found")
                if post["client_id"] != "default":
                    raise ValueError("Client publishing needs per-client account connections. Export this draft for now.")
                if not providers.enabled(post["platform"]):
                    raise ValueError("This platform is not connected. YouTube and TikTok uploads are not implemented.")
                if not post.get("approved_at") or not automation.can_publish(post):
                    raise ValueError("Explicit approval and available daily quota are required")
                # A failed attempt requires fresh approval before retrying.
                claim = store.execute(
                    "UPDATE posts SET status='publishing',error=NULL WHERE id=? AND status='scheduled'",
                    (post_id,),
                )
                if not claim.rowcount:
                    raise ValueError("This post is not approved and scheduled for publication")
                try:
                    remote_id = await providers.publish_request(post)
                    store.execute("UPDATE posts SET status='published',published_at=?,remote_id=?,error=NULL WHERE id=?",
                                  (iso(utcnow()), remote_id, post_id))
                except Exception as error:
                    message = str(error) if isinstance(error, ValueError) else "Publishing failed. Check the account before retrying."
                    store.execute("UPDATE posts SET status='failed',approved_at=NULL,error=? WHERE id=?", (message, post_id))
                    raise ValueError(message) from None
                return store.get(post_id)

        automation = Automation(store, providers, publish)
        application.state.store = store
        application.state.providers = providers
        application.state.automation = automation
        application.state.publish = publish

        async def work():
            while True:
                await asyncio.sleep(15)
                try:
                    await automation.tick()
                except Exception:
                    # Do not log provider request URLs or credentials.
                    print("Automation cycle failed; pending work remains saved.")

        task = asyncio.create_task(work()) if run_worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await providers.close()
            store.close()

    application = FastAPI(title="Contentbot", lifespan=lifespan, docs_url=None, redoc_url=None)

    @application.middleware("http")
    async def protect_api(request, call_next):
        if request.url.path.startswith("/api/"):
            expected = settings.get("APP_PASSWORD", "")
            if expected and not hmac.compare_digest(expected.encode(), request.headers.get("x-app-password", "").encode()):
                return JSONResponse({"error": "Dashboard password required"}, status_code=401)
            origin = request.headers.get("origin")
            if origin:
                try:
                    permitted = urlsplit(origin).netloc == request.headers.get("host")
                except ValueError:
                    permitted = False
                if not permitted:
                    return JSONResponse({"error": "Cross-origin requests are not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
            )
        return response

    @application.exception_handler(ValueError)
    async def invalid_request(request, error):
        return JSONResponse({"error": str(error)}, status_code=400)

    @application.get("/")
    async def dashboard(request: Request):
        return TEMPLATES.TemplateResponse(request=request, name="dashboard.html", context={
            "app_name": "Contentbot",
            "clients": [], "platforms": PLATFORMS,
        })

    @application.get("/app.js")
    async def browser_script():
        return FileResponse(ROOT / "public" / "app.js", media_type="text/javascript")

    @application.get("/style.css")
    async def browser_styles():
        return FileResponse(ROOT / "public" / "style.css", media_type="text/css")

    @application.get("/api/status")
    async def status():
        p, a = application.state.providers, application.state.automation
        return {"ai": bool(settings.get("OPENAI_API_KEY")), "whatsapp": a.whatsapp_enabled(),
                "telegram": a.telegram_enabled(), "runtime": "Python / FastAPI",
                "platforms": [{"platform": name, "connected": p.enabled(name),
                               "mode": "drafts only" if name in ("youtube", "tiktok") else "text publishing"}
                              for name in PLATFORMS]}

    @application.get("/api/clients")
    async def clients():
        return application.state.store.query("SELECT * FROM clients ORDER BY name")

    @application.post("/api/clients", status_code=201)
    async def new_client(request: Request):
        value = await read_body(request)
        name, niche = value.get("name"), value.get("niche")
        if (not isinstance(name, str) or not name.strip() or len(name) > 80
                or not isinstance(niche, str) or len(niche) > 200):
            raise ValueError("Enter a client name and niche")
        client = {"id": str(uuid4()), "name": name.strip(), "niche": niche}
        application.state.store.execute("INSERT INTO clients VALUES(?,?,?)", tuple(client.values()))
        return client

    @application.get("/api/posts")
    async def posts(client: str = "default"):
        return application.state.store.query("SELECT * FROM posts WHERE client_id=? ORDER BY created_at DESC", (client,))

    @application.post("/api/generate")
    async def generate(request: Request):
        return await application.state.providers.generate(await read_body(request))

    @application.post("/api/posts", status_code=201)
    async def create_post(request: Request):
        value = await read_body(request)
        validate_post(value)
        store, providers = application.state.store, application.state.providers
        client = value.get("client_id", "default")
        if not isinstance(client, str) or not store.one("SELECT id FROM clients WHERE id=?", (client,)):
            raise ValueError("Client not found")
        scheduled = value.get("scheduled_at")
        if scheduled and (client != "default" or not providers.enabled(value["platform"])):
            raise ValueError("Connect this platform before scheduling; you can save a draft now")
        post_id = str(uuid4())
        store.execute("INSERT INTO posts(id,platform,content,title,status,scheduled_at,created_at,client_id) VALUES(?,?,?,?,?,?,?,?)",
                      (post_id, value["platform"], value["content"].strip(), value.get("title", ""),
                       "pending_approval" if scheduled else "draft", iso(parse_time(scheduled)) if scheduled else None,
                       iso(utcnow()), client))
        post = store.get(post_id)
        if scheduled:
            application.state.automation.notify(post)
        return post

    @application.patch("/api/posts/{post_id}")
    async def edit_post(post_id: str, request: Request):
        store = application.state.store
        post = store.get(post_id)
        if not post:
            return JSONResponse({"error": "Post not found"}, status_code=404)
        if post["status"] in ("published", "publishing"):
            raise ValueError("Published posts cannot be edited")
        value = await read_body(request)
        validate_post(value)
        # The scheduler can claim the post while its request body is arriving.
        post = store.get(post_id)
        if not post or post["status"] in ("published", "publishing"):
            raise ValueError("This post can no longer be edited")
        scheduled = value.get("scheduled_at")
        if scheduled and (post["client_id"] != "default" or not application.state.providers.enabled(value["platform"])):
            raise ValueError("Connect this platform before scheduling")
        updated = store.execute("UPDATE posts SET platform=?,content=?,title=?,scheduled_at=?,status=?,approved_at=NULL,error=NULL,revision=revision+1 WHERE id=? AND status NOT IN ('published','publishing')",
                      (value["platform"], value["content"].strip(), value.get("title", ""),
                       iso(parse_time(scheduled)) if scheduled else None, "pending_approval" if scheduled else "draft", post_id))
        if not updated.rowcount:
            raise ValueError("This post can no longer be edited")
        post = store.get(post_id)
        if scheduled:
            application.state.automation.notify(post)
        return post

    @application.delete("/api/posts/{post_id}")
    async def delete_post(post_id: str):
        store = application.state.store
        post = store.get(post_id)
        if not post:
            return JSONResponse({"error": "Post not found"}, status_code=404)
        if post["status"] == "publishing":
            raise ValueError("Publishing is in progress")
        store.execute("DELETE FROM posts WHERE id=?", (post_id,))
        return {"deleted": True}

    @application.post("/api/posts/{post_id}/publish")
    async def publish_now(post_id: str):
        return await application.state.publish(post_id)

    @application.post("/api/posts/{post_id}/submit")
    async def submit(post_id: str):
        return application.state.automation.submit(post_id)

    @application.post("/api/posts/{post_id}/approve")
    async def approve(post_id: str, request: Request):
        value = await read_body(request)
        revision = value.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("Review the current post before approving")
        return application.state.automation.decide(post_id, "approve", revision)

    @application.post("/api/posts/{post_id}/reject")
    async def reject(post_id: str, request: Request):
        value = await read_body(request)
        revision = value.get("revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("Review the current post before rejecting")
        return application.state.automation.decide(post_id, "reject", revision)

    @application.get("/api/plan")
    async def plan(client: str = "default"):
        return application.state.automation.read_plan(client)

    @application.put("/api/plan")
    async def save_plan(request: Request, client: str = "default"):
        return application.state.automation.save_plan(client, await read_body(request))

    @application.post("/api/plan/generate")
    async def prepare_plan(client: str = "default"):
        return await application.state.automation.fill(client)

    @application.get("/api/notifications")
    async def notifications(client: str = "default"):
        return application.state.store.query(
            "SELECT n.*,p.status,p.content,p.platform FROM notifications n JOIN posts p ON p.id=n.post_id "
            "WHERE n.client_id=? ORDER BY n.created_at DESC LIMIT 100", (client,))

    @application.get("/webhooks/whatsapp")
    async def verify_whatsapp(request: Request):
        params = request.query_params
        verify_token = settings.get("WHATSAPP_VERIFY_TOKEN")
        if (verify_token and params.get("hub.mode") == "subscribe"
                and hmac.compare_digest(verify_token.encode(), params.get("hub.verify_token", "").encode())):
            return PlainTextResponse(params.get("hub.challenge", ""))
        return PlainTextResponse("", status_code=403)

    @application.post("/webhooks/whatsapp")
    async def whatsapp_callback(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 262144:
                return PlainTextResponse("", status_code=413)
        try:
            await application.state.automation.whatsapp_webhook(bytes(raw), request.headers.get("x-hub-signature-256", ""))
        except ValueError:
            return PlainTextResponse("", status_code=403)
        return PlainTextResponse("EVENT_RECEIVED")

    return application


app = create_app()
