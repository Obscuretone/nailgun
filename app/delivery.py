import asyncio
import base64
import json
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import SMTP
from email.utils import formatdate, parseaddr

from pymysql.err import InterfaceError, OperationalError

from app.models import Message, RawMessage

log = logging.getLogger(__name__)
DATABASE_INTERRUPTED = (InterfaceError, OperationalError)


class DeliveryError(Exception):
    def __init__(self, detail, retry=False, uncertain=False):
        self.retry = retry
        self.uncertain = uncertain
        super().__init__(detail)


def deliver(settings, row):
    payload = json.loads(row["payload"])
    if "raw" in payload:
        message = RawMessage.model_validate(payload)
        mail = BytesParser(policy=SMTP).parsebytes(base64.b64decode(message.raw))
    else:
        message = Message.model_validate(payload)
        mail = EmailMessage(policy=SMTP)
        mail["From"] = message.sender
        mail["To"] = ", ".join(message.to)
        if message.cc:
            mail["Cc"] = ", ".join(message.cc)
        mail["Subject"] = message.subject
        if message.reply_to:
            mail["Reply-To"] = message.reply_to
        mail.set_content(message.text or "")
        if message.html:
            mail.add_alternative(message.html, subtype="html")
    mail["Message-ID"] = row["message_id"]
    mail["Date"] = formatdate(row["created_at"], usegmt=True)
    source = (settings.smtp_source_ip, 0) if settings.smtp_source_ip else None
    client = None
    data_started = False
    try:
        cls = smtplib.SMTP_SSL if settings.smtp_mode == "ssl" else smtplib.SMTP
        kwargs = {"context": ssl.create_default_context()} if settings.smtp_mode == "ssl" else {}
        client = cls(
            settings.smtp_host,
            settings.smtp_port,
            local_hostname=settings.smtp_helo,
            timeout=settings.smtp_timeout,
            source_address=source,
            **kwargs,
        )
        client.ehlo()
        if settings.smtp_mode == "starttls":
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        code, _ = client.mail(parseaddr(message.sender)[1])
        if code != 250:
            raise DeliveryError(f"SMTP sender rejected ({code})", retry=400 <= code < 500)
        accepted = []
        rejected = []
        for recipient in message.recipients:
            code, _ = client.rcpt(recipient)
            if code in (250, 251):
                accepted.append(recipient)
            else:
                rejected.append(code)
        if rejected:
            # Abort the entire transaction before DATA; no partial delivery or duplicate retries.
            raise DeliveryError(
                "SMTP recipient rejected; transaction aborted ("
                + ",".join(map(str, rejected))
                + ")",
                retry=all(400 <= code < 500 for code in rejected),
            )
        data_started = True
        code, _ = client.data(mail.as_bytes())
        if code != 250:
            raise DeliveryError(f"SMTP message rejected ({code})", retry=400 <= code < 500)
        return accepted
    except DeliveryError:
        raise
    except smtplib.SMTPResponseException as exc:
        raise DeliveryError(
            f"SMTP refused request ({exc.smtp_code})", retry=400 <= exc.smtp_code < 500
        ) from None
    except (OSError, smtplib.SMTPException) as exc:
        raise DeliveryError(
            type(exc).__name__, retry=not data_started, uncertain=data_started
        ) from None
    finally:
        if client:
            # A QUIT failure after a 250 must not turn success into an ambiguous retry.
            try:
                client.close()
            except OSError:
                pass


async def wait_for_stop(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def persist(stop, operation, *args, **kwargs):
    delay = 1
    while True:
        try:
            await asyncio.to_thread(operation, *args, **kwargs)
            return True
        except DATABASE_INTERRUPTED:
            log.warning("Mail queue database unavailable during %s", operation.__name__)
            if stop.is_set():
                return False
            await wait_for_stop(stop, delay)
            if stop.is_set():
                return False
            delay = min(30, delay * 2)


async def worker(store, settings, stop):
    if not await persist(stop, store.recover):
        return
    while not stop.is_set():
        try:
            row = await asyncio.to_thread(store.claim)
        except DATABASE_INTERRUPTED:
            log.warning("Mail queue database unavailable during claim")
            await wait_for_stop(stop, 1)
            # A claim may have committed before the connection failed. Reconcile its state.
            if stop.is_set() or not await persist(stop, store.recover):
                return
            continue
        if row:
            try:
                accepted = await asyncio.to_thread(deliver, settings, row)
            except DeliveryError as exc:
                status = "uncertain" if exc.uncertain else "failed"
                if exc.retry and row["attempts"] < settings.max_attempts:
                    status = "queued"
                outcome = {"error": str(exc), "delay": min(3600, 30 * 2 ** row["attempts"])}
                log.warning("Delivery %s: %s", row["id"], status)
            except Exception:
                status = "uncertain"
                outcome = {"error": "Unexpected delivery error; inspect server logs"}
                log.exception("Unexpected delivery error for %s", row["id"])
            else:
                status, outcome = "sent", {"accepted": accepted}
            if not await persist(stop, store.finish, row["id"], status, **outcome):
                return
        await wait_for_stop(stop, settings.send_interval if row else 1)
