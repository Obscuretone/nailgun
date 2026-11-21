import asyncio
import base64
import fcntl
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pymysql import MySQLError

from app.config import Settings
from app.delivery import worker
from app.mailgun import load_send_keys, permits_send, register_mailgun
from app.models import DomainUpdate, Message, RawMessage
from app.store import ServiceError, Store


class AuthAndLimits:
    def __init__(self, app, key, mailgun_send_keys):
        self.app = app
        self.key = key
        self.mailgun_send_keys = mailgun_send_keys

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        response = None
        mailgun = scope["path"].startswith("/v3/")
        if scope["path"] != "/healthz":
            raw = headers.get(b"authorization", b"").decode("latin1")
            candidate = raw[7:] if raw.lower().startswith("bearer ") else ""
            if raw.lower().startswith("basic ") and mailgun:
                try:
                    username, password = (
                        base64.b64decode(raw[6:], validate=True).decode().split(":", 1)
                    )
                    candidate = password if username == "api" else ""
                except (ValueError, UnicodeError):
                    pass
            if not (secrets.compare_digest(candidate.encode(), self.key.encode())
                    or permits_send(self.mailgun_send_keys, scope, raw, candidate)):
                response = JSONResponse(
                    {"message": "Forbidden"} if mailgun else {"detail": "Unauthorized"}, 401,
                    headers={"WWW-Authenticate": 'Basic realm="Mailgun"' if mailgun else "Bearer"},
                )
        # Raw MIME submissions may contain attachments; other endpoints remain bounded.
        body_limit = 8 * 1024 * 1024 if mailgun or scope["path"] == "/v1/messages/raw" else 524288
        body = bytearray()
        if not response:
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                body.extend(event.get("body", b""))
                if len(body) > body_limit:
                    response = JSONResponse({"detail": "Request body exceeds endpoint limit"}, 413)
                    break
                if not event.get("more_body"):
                    break
        if response:
            return await response(scope, receive, send)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def secure_send(event):
            if event["type"] == "http.response.start":
                event.setdefault("headers", []).append((b"cache-control", b"no-store"))
            await send(event)

        await self.app(scope, replay, secure_send)


def create_app(settings=None):
    settings = settings or Settings()
    store = Store(settings)
    mcp = FastMCP(
        "VPS Mail",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[settings.public_host, "localhost:*", "127.0.0.1:*", "testserver"],
            allowed_origins=[f"https://{settings.public_host}"],
        ),
    )

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )
    def send_email(message: Message, idempotency_key: str | None = None) -> dict[str, Any]:
        """Send an email through Google SMTP. Queues a real external email. Use only when the user explicitly authorizes sending; supply an idempotency key for retries."""
        return store.enqueue(message, idempotency_key)

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_email_status(id: str) -> dict[str, Any]:
        """Check queue/delivery status. Sent means Google accepted SMTP, not inbox delivery."""
        return store.get(id)

    @mcp.tool(annotations={"readOnlyHint": True})
    def list_emails(limit: int = 50) -> list[dict[str, Any]]:
        """List recent email delivery metadata, excluding bodies."""
        return store.list(limit)

    @mcp.tool(annotations={"readOnlyHint": True})
    def list_domains() -> list[dict[str, Any]]:
        """List server domains and Google verification/enablement status."""
        return store.domains()

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False})
    def cancel_email(id: str) -> dict[str, Any]:
        """Cancel a queued email before delivery begins."""
        return store.cancel(id)

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        with (settings.state_directory / "worker.lock").open("a") as lock:
            if settings.worker_enabled:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            async with mcp.session_manager.run():
                task = (
                    asyncio.create_task(worker(store, settings, stop))
                    if settings.worker_enabled
                    else None
                )
                app.state.delivery_task = task
                try:
                    yield
                finally:
                    stop.set()
                    if task:
                        await task

    app = FastAPI(
        title="VPS Mail",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/mail/docs",
        openapi_url="/mail/openapi.json",
        redoc_url=None,
    )
    app.state.store = store
    app.add_middleware(
        AuthAndLimits, key=settings.api_key.get_secret_value(),
        mailgun_send_keys=load_send_keys(settings.mailgun_send_keys_file),
    )

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        if request.url.path.startswith("/v3/"):
            return JSONResponse({"message": exc.detail}, exc.status)
        return JSONResponse({"detail": exc.detail}, exc.status)

    @app.get("/healthz")
    def health():
        task = getattr(app.state, "delivery_task", None)
        try:
            if (task is None or not task.done()) and store.ping():
                return {"status": "ok"}
        except MySQLError:
            pass
        return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.post("/v1/messages", status_code=202)
    def send_message(
        message: Message, idempotency_key: Annotated[str | None, Header(max_length=200)] = None
    ):
        return store.enqueue(message, idempotency_key)

    @app.get("/v1/messages")
    def messages(limit: int = Query(50, ge=1, le=100)):
        return {"items": store.list(limit)}

    @app.post("/v1/messages/raw", status_code=202)
    def send_raw(
        message: RawMessage, idempotency_key: Annotated[str | None, Header(max_length=200)] = None
    ):
        return store.enqueue(message, idempotency_key)

    @app.get("/v1/messages/{identifier}")
    def get_message(identifier: str):
        return store.get(identifier)

    @app.delete("/v1/messages/{identifier}")
    def cancel_message(identifier: str):
        return store.cancel(identifier)

    @app.get("/v1/domains")
    def domains():
        return {"items": store.domains()}

    @app.put("/v1/domains/{name}")
    def update_domain(name: str, update: DomainUpdate):
        return store.update_domain(name, update)

    register_mailgun(app, store)

    app.mount("/mcp", mcp.streamable_http_app())
    return app
