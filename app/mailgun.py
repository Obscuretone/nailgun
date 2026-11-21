"""Mailgun's outbound message request and response contract."""

import base64
import json
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
from hashlib import sha256
from hmac import compare_digest

from fastapi import Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from .models import MAX_MIME_BYTES, Message, RawMessage
from .store import ServiceError

SEND_PATH = re.compile(r"/v3/([A-Za-z0-9.-]{1,253})/messages(?:\.mime)?\Z")


def load_send_keys(path):
    if path is None:
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or any(
        not re.fullmatch(r"[a-z0-9.-]{1,253}", domain) or not isinstance(keys, list)
        or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key) for key in keys)
        for domain, keys in data.items()
    ):
        raise ValueError("Supply valid domain-scoped Mailgun send keys.")
    return {domain: [key.encode() for key in keys] for domain, keys in data.items()}


def permits_send(keys, scope, authorization, candidate):
    match = SEND_PATH.fullmatch(scope["path"])
    return bool(match and scope["method"] == "POST" and authorization.lower().startswith("basic ") and any(
        compare_digest(candidate.encode(), key) for key in keys.get(match[1].lower(), ())
    ))


FORM_FIELDS = {"from", "to", "cc", "bcc", "subject", "text", "html", "h:Reply-To", "attachment", "inline"}


def recipients(form, field):
    values = form.getlist(field)
    if any(not isinstance(value, str) for value in values):
        raise ValueError("Invalid recipient")
    return [f"{name} <{address}>" if name else address for name, address in getaddresses(values)]


async def attached_message(message, form):
    mime = EmailMessage()
    mime["From"] = message.sender
    mime["To"] = ", ".join(message.to)
    mime["Subject"] = message.subject
    if message.cc:
        mime["Cc"] = ", ".join(message.cc)
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    mime.set_content(message.text or "")
    if message.html:
        mime.add_alternative(message.html, subtype="html")
    for kind in ("inline", "attachment"):
        for file in form.getlist(kind):
            if not isinstance(file, UploadFile) or not file.filename:
                raise ValueError("Invalid attachment")
            data = await file.read(MAX_MIME_BYTES + 1)
            content = file.content_type or "application/octet-stream"
            if content.startswith("multipart/") or "/" not in content:
                content = "application/octet-stream"
            maintype, subtype = content.split(";", 1)[0].split("/", 1)
            args = {"maintype": maintype, "subtype": subtype, "filename": file.filename}
            if kind == "inline":
                if not message.html:
                    raise ValueError("Inline files require HTML")
                mime.get_body(preferencelist=("html",)).add_related(data, cid=f"<{file.filename}>", **args)
            else:
                mime.add_attachment(data, **args)
    for index, part in enumerate(mime.walk()):
        if part.is_multipart():
            content = b"\0".join(child.get_payload(decode=True) or b"" for child in part.walk())
            part.set_boundary(f"nailgun-{index}-{sha256(content).hexdigest()[:32]}")
    return RawMessage.model_validate({
        "from": message.sender, "to": message.recipients,
        "raw": base64.b64encode(mime.as_bytes(policy=policy.SMTP)).decode(),
    })


def register_mailgun(app, store):
    async def enqueue(request, domain, message):
        if message.domain != domain.lower():
            raise ServiceError(400, "Sender domain must match the URL domain")
        result = await run_in_threadpool(store.enqueue, message, request.headers.get("Idempotency-Key"))
        return {"id": result["message_id"], "message": "Queued. Thank you."}

    @app.post("/v3/{domain_name}/messages")
    async def send_message(domain_name: str, request: Request):
        try:
            async with request.form(max_files=20, max_fields=310, max_part_size=400_000) as form:
                unsupported = set(form) - FORM_FIELDS
                if unsupported:
                    raise ServiceError(400, "Unsupported Mailgun fields: " + ", ".join(sorted(unsupported)))
                values = {key: form[key] for key in ("from", "subject", "text", "html") if key in form}
                values.update({field: recipients(form, field) for field in ("to", "cc", "bcc")})
                if "h:Reply-To" in form:
                    values["reply_to"] = form["h:Reply-To"]
                message = Message.model_validate(values)
                if "attachment" in form or "inline" in form:
                    message = await attached_message(message, form)
        except (ValueError, TypeError, HTTPException):
            raise ServiceError(400, "Invalid sender, recipients, message body or attachment") from None
        return await enqueue(request, domain_name, message)

    @app.post("/v3/{domain_name}/messages.mime")
    async def send_mime(domain_name: str, request: Request):
        try:
            async with request.form(max_files=1, max_fields=101, max_part_size=400_000) as form:
                file = form.get("message")
                if set(form) - {"to", "message"} or not isinstance(file, UploadFile):
                    raise ValueError("Expected a MIME file")
                content = await file.read(MAX_MIME_BYTES + 1)
                if len(content) > MAX_MIME_BYTES:
                    raise ValueError("MIME message exceeds size limit")
                headers = BytesParser(policy=policy.default).parsebytes(content, headersonly=True)
                message = RawMessage.model_validate({
                    "from": str(headers.get("From", "")), "to": recipients(form, "to"),
                    "raw": base64.b64encode(content).decode(),
                })
        except (ValidationError, ValueError, TypeError, HTTPException):
            raise ServiceError(400, "Invalid MIME message or recipients") from None
        return await enqueue(request, domain_name, message)
