from email.message import EmailMessage
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_mailgun_accepts_upstream_multipart_shapes(tmp_path, monkeypatch):
    store = Mock()
    store.enqueue.return_value = {"message_id": "<test@example.com>", "id": "private-queue-id"}
    monkeypatch.setattr("app.main.Store", lambda settings: store)
    key = "API-fixture-" * 4
    credential = tmp_path / "unused-db-password"
    credential.write_text("unused-fixture")
    domain_key = "send_" + "X" * 40
    keys_file = tmp_path / "mailgun-keys.json"
    keys_file.write_text('{"example.com": ["' + domain_key + '"]}')
    settings = Settings(
        mailgun_send_keys_file=keys_file, api_key=key, state_directory=tmp_path, worker_enabled=False, _env_file=None,
        mariadb_database="fixture", mariadb_user="fixture", mariadb_password_file=credential,
    )
    with TestClient(create_app(settings)) as client:
        endpoint = "/v3/example.com/messages"
        assert client.post(endpoint).status_code == 401
        client.auth = ("api", domain_key)
        assert client.get("/v1/messages").status_code == 401
        assert client.post("/v3/other.example/messages", data={"text": "Fixture"}).status_code == 401
        fields = {"from": "Demo <mail@example.com>", "to": ["one@example.net", "two@example.net"],
                  "subject": "Contract", "text": "Fixture text"}
        result = client.post(endpoint, files=[("attachment", ("brief.txt", b"Attachment", "text/plain"))],
                             data=fields, headers={"Idempotency-Key": "attachment-fixture"})
        assert result.status_code == 200, result.text
        assert result.json() == {"id": "<test@example.com>", "message": "Queued. Thank you."}
        first = store.enqueue.call_args.args[0]
        result = client.post(endpoint, files=[("attachment", ("brief.txt", b"Attachment", "text/plain"))],
                             data=fields, headers={"Idempotency-Key": "attachment-fixture"})
        assert result.status_code == 200 and first == store.enqueue.call_args.args[0]
        mail = EmailMessage()
        mail["From"], mail["To"], mail["Subject"] = "mail@example.com", "one@example.net", "MIME fixture"
        mail.set_content("Fixture")
        result = client.post(endpoint + ".mime", data={"to": "one@example.net"},
                             files={"message": ("mail.eml", mail.as_bytes(), "message/rfc822")})
        assert result.status_code == 200, result.text
        assert client.post(endpoint, data={"o:unsupported": "true"}).status_code == 400
