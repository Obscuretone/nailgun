import base64
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest
from pydantic import ValidationError
from test_service import BODY, FakeSMTP
from test_service import client as client
from test_service import settings as settings
from test_service import store as store

from app.delivery import deliver
from app.local_broker import submission
from app.models import Message, RawMessage
from app.store import ServiceError
from integrations.sendmail import arguments, request


def mime(date="Thu, 12 Nov 2026 12:00:00 +0000"):
    mail = EmailMessage()
    mail["From"] = "Example <hello@example.com>"
    mail["To"] = "alice@example.net"
    mail["Bcc"] = "secret@example.net"
    mail["Reply-To"] = "customer@example.org"
    mail["Date"] = date
    mail["Message-ID"] = "<event-1@example.com>"
    mail["Subject"] = "A résumé attachment"
    mail.set_content("Plain text")
    mail.add_alternative("<p>HTML body</p>", subtype="html")
    mail.add_attachment(
        b"\x00\xff\x10 binary data",
        maintype="application",
        subtype="octet-stream",
        filename="file.bin",
    )
    # Fixed boundaries make this fixture represent a retry of the same email.
    mail.set_boundary("outer-boundary")
    mail.get_payload(0).set_boundary("inner-boundary")
    return mail.as_bytes()


def raw_message(raw=None, **extra):
    return RawMessage.model_validate(
        {
            "from": "hello@example.com",
            "to": ["alice@example.net", "secret@example.net"],
            "raw": base64.b64encode(raw or mime()).decode(),
            **extra,
        }
    )


def test_raw_mime_attachment_privacy_and_canonical_retry(store, settings, monkeypatch):
    first = raw_message()
    second = raw_message(mime("Fri, 13 Nov 2026 12:00:00 +0000"))
    assert first.model_dump() == second.model_dump()
    result = store.enqueue(first, "mime-one")
    assert store.enqueue(second, "mime-one")["id"] == result["id"]
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    row = store.claim()
    assert deliver(settings, row) == first.to
    data = next(call for call in FakeSMTP.instances[-1].calls if isinstance(call, bytes))
    mail = BytesParser(policy=policy.default).parsebytes(data)
    assert mail["Bcc"] is None and b"secret@example.net" not in data
    assert mail["Reply-To"] == "customer@example.org"
    assert list(mail.iter_attachments())[0].get_payload(decode=True) == b"\x00\xff\x10 binary data"
    assert str(mail["Message-ID"]) == result["message_id"]


def test_raw_api(client):
    data = raw_message().model_dump(by_alias=True)
    response = client.post("/v1/messages/raw", json=data)
    assert response.status_code == 202, response.text
    assert client.post("/v1/messages/raw", json=dict(data, raw="broken")).status_code == 422
    assert (
        client.post(
            "/v1/messages/raw", json=dict(data, **{"from": "spoof@other.example"})
        ).status_code
        == 422
    )


def test_sendmail_flags_and_recipient_extraction():
    data = request(mime(), ["-t", "-i", "-fhello@example.com", "--", "extra@example.net"])
    assert data["recipients"] == ["extra@example.net", "alice@example.net", "secret@example.net"]
    assert data["sender"] == "hello@example.com"
    assert arguments(["-F", "Cron daemon", "-odi", "-t", "--check"]) == ([], None, True, True)
    with pytest.raises(ValueError):
        arguments(["-f"])
    with pytest.raises(ValueError):
        arguments(["--unknown"])
    with pytest.raises(ValueError):
        request(b"From: hello@example.com\n\nbody", [])


def test_local_uid_policy_and_aliases():
    config = {
        "users": {"site": {"domains": ["example.com"], "default_from": "alerts@example.com"}},
        "aliases": {"root": "admin@example.net"},
    }
    data = request(mime(), ["-t", "-i"])
    message, key = submission(data, "site", config)
    assert message.domain == "example.com" and key.startswith("local-")
    with pytest.raises(ServiceError):
        submission(data, "unknown", config)
    with pytest.raises(ServiceError):
        submission(
            data,
            "site",
            {
                "users": {
                    "site": {"domains": ["other.example"], "default_from": "alerts@other.example"}
                }
            },
        )
    data = request(b"From: root@servitro\nTo: root\nSubject: Cron\n\njob output", ["-t"])
    message, _ = submission(data, "site", config)
    assert message.sender == "alerts@example.com" and message.to == ["admin@example.net"]


def test_unsupported_mime_and_storage_cap(store, settings):
    with pytest.raises(ValidationError):
        raw_message(b"From: hello@example.com\nResent-To: hidden@example.net\n\nbody")
    settings.max_storage_bytes = 1
    with pytest.raises(ServiceError) as exc:
        store.enqueue(Message.model_validate(BODY))
    assert exc.value.status == 429


def test_local_idempotency_body_change_detected(store):
    config = {"users": {"site": {"domains": ["example.com"], "default_from": "hello@example.com"}}}
    message, key = submission(request(mime(), ["-t"]), "site", config)
    first = store.enqueue(message, key)
    assert store.enqueue(message, key)["id"] == first["id"]
    altered = raw_message(mime().replace(b"Plain text", b"Other text"))
    with pytest.raises(ServiceError) as exc:
        store.enqueue(altered, key)
    assert exc.value.status == 409
