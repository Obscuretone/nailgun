"""Credential-free local submissions authenticated by Linux Unix-socket peer UID."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import pwd
import socket
import struct
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings
from app.models import MAX_MIME_BYTES, RawMessage, mailbox
from app.store import ServiceError, Store

MAX_FRAME = 8 * 1024 * 1024
log = logging.getLogger(__name__)


def submission(data, username, config):
    rule = config["users"].get(username)
    if not rule:
        raise ServiceError(403, "Local account is not authorized to submit mail")
    if set(data) - {"raw", "recipients", "sender", "check"}:
        raise ServiceError(422, "Unsupported local submission fields")
    try:
        raw = base64.b64decode(data["raw"], validate=True)
    except (ValueError, KeyError, TypeError):
        raise ServiceError(422, "Invalid MIME data") from None
    if len(raw) > MAX_MIME_BYTES:
        raise ServiceError(413, "MIME message exceeds 5 MiB")
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    if len(mail.get_all("From", [])) > 1:
        raise ServiceError(422, "Multiple From headers are not supported")
    sender = str(mail.get("From", ""))
    address = parseaddr(sender)[1]
    # Cron and PHP defaults such as root@servitro are local identities.
    if (
        not address
        or "@" not in address
        or "." not in address.rsplit("@", 1)[1]
        or address.endswith("@localhost")
    ):
        if "From" in mail:
            del mail["From"]
        sender = rule["default_from"]
        mail["From"] = sender
    sender = mailbox(sender)
    envelope = data.get("sender") or parseaddr(sender)[1]
    if "@" not in envelope or "." not in envelope.rsplit("@", 1)[1]:
        envelope = parseaddr(sender)[1]
    recipients = data.get("recipients")
    if isinstance(recipients, list):
        recipients = [config.get("aliases", {}).get(r, r) for r in recipients]
    message = RawMessage.model_validate(
        {
            "from": envelope,
            "to": recipients,
            "raw": base64.b64encode(mail.as_bytes(policy=policy.SMTP)).decode(),
        }
    )
    allowed = rule["domains"]
    if allowed != ["*"] and message.domain not in allowed:
        raise ServiceError(403, "Local account cannot send from this domain")
    key = None
    if message.client_reference:
        key = (
            "local-"
            + hashlib.sha256((username + "\0" + message.client_reference).encode()).hexdigest()
        )
    return message, key


async def serve(settings=None, config_path=None):
    settings = settings or Settings()
    config = json.loads(Path(config_path or "/etc/vps-mail/local.json").read_text())
    store = Store(settings)
    path = Path(config.get("socket", "/run/vps-mail-local/mail.sock"))
    semaphore = asyncio.Semaphore(4)

    async def handle(reader, writer):
        async with semaphore:
            try:
                peer = writer.get_extra_info("socket")
                _, uid, _ = struct.unpack(
                    "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                username = pwd.getpwuid(uid).pw_name
                if not config["users"].get(username):
                    raise ServiceError(403, "Local account is not authorized to submit mail")
                length = struct.unpack("!I", await asyncio.wait_for(reader.readexactly(4), 15))[0]
                if length > MAX_FRAME:
                    raise ServiceError(413, "Local submission exceeds 8 MiB")
                data = json.loads(await asyncio.wait_for(reader.readexactly(length), 30))
                message, key = submission(data, username, config)
                if data.get("check"):
                    domain = next((d for d in store.domains() if d["name"] == message.domain), None)
                    if not domain or not all(
                        domain[k] for k in ("enabled", "google_verified", "present")
                    ):
                        raise ServiceError(403, "Sender domain is not enabled")
                    result = {
                        "status": "validated",
                        "domain": message.domain,
                        "recipient_count": len(message.recipients),
                    }
                else:
                    result = await asyncio.to_thread(store.enqueue, message, key)
                    log.info("Local mail account=%s queue=%s", username, result["id"])
                result = {"ok": True, **result}
            except ServiceError as exc:
                result = {"ok": False, "status_code": exc.status, "error": exc.detail}
            except (ValueError, KeyError, TypeError, ValidationError):
                result = {"ok": False, "status_code": 422, "error": "Invalid local mail request"}
            except (OSError, asyncio.IncompleteReadError, TimeoutError):
                result = {
                    "ok": False,
                    "status_code": 503,
                    "error": "Local mail submission interrupted",
                }
            except Exception:
                log.exception("Local mail broker error")
                result = {
                    "ok": False,
                    "status_code": 503,
                    "error": "Local mail service unavailable",
                }
            try:
                payload = json.dumps(result).encode()
                writer.write(struct.pack("!I", len(payload)) + payload)
                await writer.drain()
            except (OSError, ConnectionError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()

    path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(handle, path=path, backlog=32)
    os.chmod(path, 0o660)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
