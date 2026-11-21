import asyncio
import os
import smtplib
import uuid
from pathlib import Path

import httpx
import pytest
import pymysql
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.config import Settings
from app.delivery import DeliveryError, deliver, worker
from app.main import create_app
from app.models import DomainUpdate, Message
from app.store import ServiceError, Store
from app.sync_domains import inventory

KEY = "testing-key-" * 4
BODY = {
    "from": "My app <hello@example.com>",
    "to": ["alice@example.net"],
    "subject": "Hello",
    "text": "A test email",
    "bcc": ["secret@example.net"],
}


@pytest.fixture
def settings(tmp_path):
    socket = os.environ.get("NAILGUN_TEST_SOCKET")
    if not socket:
        pytest.skip("Set NAILGUN_TEST_SOCKET to an isolated development MariaDB server")
    name = "nailgun_test_" + uuid.uuid4().hex[:16]
    credential = tmp_path / "test.password"
    password = uuid.uuid4().hex
    credential.write_text(password)
    schema = Path(__file__).resolve().parents[1] / "deploy" / "schema.sql"
    with pymysql.connect(unix_socket=socket, user="root", autocommit=True) as admin:
        with admin.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{name}`")
            try:
                cursor.execute(f"USE `{name}`")
                for statement in schema.read_text().split(";"):
                    if statement.strip():
                        cursor.execute(statement)
                cursor.execute("CREATE USER %s@'localhost' IDENTIFIED BY %s", (name, password))
                cursor.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{name}`.* TO %s@'localhost'", (name,))
                yield Settings(
                    api_key=KEY, state_directory=tmp_path, mariadb_database=name, mariadb_user=name,
                    mariadb_socket=socket, mariadb_password_file=credential, worker_enabled=False,
                    smtp_source_ip=None, send_interval=0, daily_recipient_limit=10, _env_file=None,
                )
            finally:
                cursor.execute("DROP USER IF EXISTS %s@'localhost'", (name,))
                cursor.execute(f"DROP DATABASE `{name}`")


@pytest.fixture
def store(settings):
    store = Store(settings)
    store.reconcile(["example.com"])
    store.update_domain("example.com", DomainUpdate(enabled=True, google_verified=True))
    return store


@pytest.fixture
def client(settings, store):
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = f"Bearer {KEY}"
        yield client


def test_auth_and_validation(client):
    assert client.get("/healthz", headers={"Authorization": ""}).status_code == 200
    for path in ["/v1/messages", "/v1/domains", "/mail/docs", "/mail/openapi.json", "/mcp/"]:
        assert client.get(path, headers={"Authorization": ""}).status_code == 401
    for bad in [
        dict(BODY, **{"from": ""}),
        dict(BODY, subject="Hi\r\nBcc: victim@example.net"),
        dict(BODY, **{"from": "hello@unverified.example"}),
        dict(BODY, text="", html=""),
        dict(BODY, to=["x\n@example.net"]),
    ]:
        assert client.post("/v1/messages", json=bad).status_code in (403, 422)
    assert client.post("/v1/messages", content=b"x" * 524289).status_code == 413


def test_queue_idempotency_and_quota(client):
    r = client.post("/v1/messages", json=BODY, headers={"Idempotency-Key": "one"})
    assert r.status_code == 202
    identifier = r.json()["id"]
    assert (
        client.post("/v1/messages", json=BODY, headers={"Idempotency-Key": "one"}).json()["id"]
        == identifier
    )
    assert (
        client.post(
            "/v1/messages", json=dict(BODY, text="different"), headers={"Idempotency-Key": "one"}
        ).status_code
        == 409
    )
    assert "payload" not in client.get(f"/v1/messages/{identifier}").json()
    for _ in range(4):
        assert client.post("/v1/messages", json=BODY).status_code == 202
    assert client.post("/v1/messages", json=BODY).status_code == 429
    assert client.delete(f"/v1/messages/{identifier}").json()["status"] == "cancelled"
    assert client.delete(f"/v1/messages/{identifier}").status_code == 409


def test_inventory_sync_preserves_flags_and_disables_removed(store):
    store.reconcile(["example.com", "new.example.com"])
    assert next(d for d in store.domains() if d["name"] == "example.com")["enabled"]
    with pytest.raises(ServiceError):
        store.enqueue(Message.model_validate(dict(BODY, **{"from": "hello@new.example.com"})))
    store.reconcile(["new.example.com"])
    with pytest.raises(ServiceError):
        store.enqueue(Message.model_validate(BODY))
    assert inventory(
        "server_name example.com www.example.com;\n server_name new.example.com _ *.example.net;"
    ) == ["example.com", "new.example.com"]


def test_mailgun_subset(client):
    r = client.post(
        "/v3/example.com/messages",
        auth=("api", KEY),
        data={
            "from": "hello@example.com",
            "to": "A <alice@example.net>, bob@example.net",
            "subject": "Mailgun test",
            "text": "Test",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["message"] == "Queued. Thank you."
    assert set(r.json()) == {"id", "message"}
    assert client.post("/v3/example.com/messages", data={"o:tracking": "yes"}).status_code == 400
    assert (
        client.post(
            "/v3/other.example/messages",
            data={
                "from": "hello@example.com",
                "to": "alice@example.net",
                "subject": "Test",
                "text": "Test",
            },
        ).status_code
        == 400
    )


class FakeSMTP:
    instances = []
    rcpt_code = 250
    data_error = None
    data_code = 250

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, **kwargs):
        self.calls.append("tls")

    def mail(self, sender):
        self.calls.append(sender)
        return 250, b"ok"

    def rcpt(self, recipient):
        self.calls.append(recipient)
        return self.rcpt_code, b"ok"

    def data(self, body):
        self.calls.append(body)
        if self.data_error:
            raise self.data_error
        return self.data_code, b"ok"

    def close(self):
        self.calls.append("close")


def test_smtp_tls_bcc_and_ambiguous_failure(monkeypatch, settings, store):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    message = store.enqueue(Message.model_validate(BODY))
    row = store.claim()
    accepted = deliver(settings, row)
    assert accepted == ["alice@example.net", "secret@example.net"]
    calls = FakeSMTP.instances[-1].calls
    assert calls[:3] == ["ehlo", "tls", "ehlo"]
    body = next(v for v in calls if isinstance(v, bytes))
    assert b"Bcc:" not in body and b"secret@example.net" not in body
    assert message["message_id"].encode() in body
    monkeypatch.setattr(FakeSMTP, "data_error", TimeoutError())
    with pytest.raises(DeliveryError) as exc:
        deliver(settings, row)
    assert exc.value.uncertain and not exc.value.retry
    monkeypatch.setattr(FakeSMTP, "data_error", None)
    monkeypatch.setattr(FakeSMTP, "rcpt_code", 450)
    with pytest.raises(DeliveryError) as exc:
        deliver(settings, row)
    assert exc.value.retry and not exc.value.uncertain
    assert not any(isinstance(v, bytes) for v in FakeSMTP.instances[-1].calls)


def test_recovery_and_single_claim(store):
    first = store.enqueue(Message.model_validate(BODY))
    assert store.claim()["id"] == first["id"]
    assert store.claim() is None
    store.recover()
    assert store.get(first["id"])["status"] == "uncertain"
    assert store.claim() is None


@pytest.mark.asyncio
async def test_worker_retries_safe_errors(monkeypatch, settings, store):
    stop = asyncio.Event()
    row = store.enqueue(Message.model_validate(BODY))

    def failure(*args):
        stop.set()
        raise DeliveryError("temporary refusal", retry=True)

    monkeypatch.setattr("app.delivery.deliver", failure)
    await worker(store, settings, stop)
    result = store.get(row["id"])
    assert result["status"] == "queued" and result["attempts"] == 1


@pytest.mark.asyncio
async def test_worker_retries_result_write_without_resending(monkeypatch, settings, store):
    from pymysql.err import OperationalError

    stop = asyncio.Event()
    row = store.enqueue(Message.model_validate(BODY))
    sent, finished = [], []
    original = store.finish

    def deliver_once(*args):
        sent.append(args[1]["id"])
        return ["alice@example.net"]

    def finish_with_lost_ack(*args, **kwargs):
        finished.append(args[0])
        original(*args, **kwargs)
        if len(finished) == 1:
            raise OperationalError(2013, "Lost database acknowledgement")
        stop.set()

    monkeypatch.setattr("app.delivery.deliver", deliver_once)
    monkeypatch.setattr(store, "finish", finish_with_lost_ack)
    await asyncio.wait_for(worker(store, settings, stop), timeout=4)
    assert sent == [row["id"]]
    assert finished == [row["id"], row["id"]]
    assert store.get(row["id"])["status"] == "sent"


@pytest.mark.asyncio
async def test_mcp_protocol(settings, store):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), headers={"Authorization": f"Bearer {KEY}"}
        ) as http:
            async with streamable_http_client(
                "http://testserver/mcp/", http_client=http
            ) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    assert {t.name for t in result.tools} == {
                        "send_email",
                        "get_email_status",
                        "list_emails",
                        "list_domains",
                        "cancel_email",
                    }
                    result = await session.call_tool(
                        "send_email", {"message": BODY, "idempotency_key": "mcp-test"}
                    )
                    assert not result.isError, result
                    content = result.structuredContent
                    assert content["status"] == "queued"
                    result = await session.call_tool("get_email_status", {"id": content["id"]})
                    assert result.structuredContent["status"] == "queued"
                    result = await session.call_tool(
                        "send_email", {"message": dict(BODY, **{"from": "no@other.example"})}
                    )
                    assert result.isError
