import base64
from email import policy
from email.parser import BytesParser
from email.utils import formataddr, parseaddr
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


def mailbox(value: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Header control characters are not allowed")
    name, address = parseaddr(value, strict=True)
    from pydantic import TypeAdapter

    address = str(TypeAdapter(EmailStr).validate_python(address))
    # SMTPUTF8 is intentionally not supported by this relay.
    address.encode("ascii")
    return formataddr((name, address)) if name else address


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    sender: str = Field(alias="from", min_length=1, max_length=320)
    to: Annotated[list[str], Field(min_length=1, max_length=100)]
    cc: list[str] = Field(default_factory=list, max_length=100)
    bcc: list[str] = Field(default_factory=list, max_length=100)
    subject: str = Field(min_length=1, max_length=998)
    text: str | None = Field(default=None, max_length=200_000)
    html: str | None = Field(default=None, max_length=200_000)
    reply_to: str | None = Field(default=None, max_length=320)

    @field_validator("sender", "reply_to")
    @classmethod
    def valid_mailbox(cls, value):
        return None if value is None else mailbox(value)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def valid_recipients(cls, values):
        return [mailbox(v) for v in values]

    @field_validator("subject")
    @classmethod
    def valid_subject(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Header control characters are not allowed")
        return value

    @model_validator(mode="after")
    def valid_body(self):
        if not self.text and not self.html:
            raise ValueError("Provide text or html")
        if len(self.recipients) > 100:
            raise ValueError("At most 100 unique recipients per message")
        return self

    @property
    def recipients(self):
        return list(dict.fromkeys(parseaddr(v)[1] for v in self.to + self.cc + self.bcc))

    @property
    def domain(self):
        return parseaddr(self.sender)[1].rsplit("@", 1)[1].lower()


class DomainUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    google_verified: bool = False
    notes: str = Field(default="", max_length=2000)


MAX_MIME_BYTES = 5 * 1024 * 1024


class RawMessage(BaseModel):
    """RFC 5322 content plus a separate SMTP envelope, preserving attachments."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    sender: str = Field(alias="from", max_length=320)
    to: list[str] = Field(min_length=1, max_length=100)
    raw: str = Field(max_length=7_100_000)
    client_reference: str | None = Field(default=None, max_length=998)

    @field_validator("sender")
    @classmethod
    def valid_sender(cls, value):
        return mailbox(value)

    @field_validator("to")
    @classmethod
    def valid_to(cls, values):
        return list(dict.fromkeys(parseaddr(mailbox(v))[1] for v in values))

    @model_validator(mode="after")
    def canonical_mime(self):
        try:
            data = base64.b64decode(self.raw, validate=True)
        except ValueError:
            raise ValueError("raw must be valid base64") from None
        if len(data) > MAX_MIME_BYTES:
            raise ValueError("MIME message exceeds 5 MiB")
        mail = BytesParser(policy=policy.default).parsebytes(data)
        if mail.defects or len(mail.get_all("From", [])) != 1:
            raise ValueError("MIME requires one valid From header and a header/body separator")
        sender = mailbox(str(mail["From"]))
        if parseaddr(sender)[1].rsplit("@", 1)[1].lower() != self.domain:
            raise ValueError("MIME From and envelope sender domains must match")
        if any(header.lower().startswith("resent-") for header in mail.keys()):
            raise ValueError("Resent-* headers are not supported")
        reference = self.client_reference or str(mail.get("Message-ID", "")) or None
        if reference and (len(reference) > 998 or any(ord(c) < 32 for c in reference)):
            raise ValueError("Invalid client message reference")
        self.client_reference = reference
        # Date and Message-ID are assigned from the durable queue. Removing them
        # here makes retries with the same event reference/content idempotent.
        for header in list(mail.keys()):
            if header.lower() in {
                "bcc",
                "date",
                "message-id",
                "dkim-signature",
                "return-path",
                "received",
            } or header.lower().startswith("arc-"):
                del mail[header]
        normalized = mail.as_bytes(policy=policy.SMTP)
        if len(normalized) > MAX_MIME_BYTES:
            raise ValueError("Normalized MIME message exceeds 5 MiB")
        self.raw = base64.b64encode(normalized).decode()
        return self

    @property
    def recipients(self):
        return self.to

    @property
    def domain(self):
        return parseaddr(self.sender)[1].rsplit("@", 1)[1].lower()
