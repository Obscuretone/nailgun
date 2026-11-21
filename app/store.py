import hashlib
import json
import time
import uuid
from contextlib import contextmanager

import pymysql

from app.models import Message, RawMessage

PUBLIC_MESSAGE_COLUMNS = (
    "id, message_id, domain, status, attempts, created_at, updated_at, next_attempt, error, accepted"
)


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


class Store:
    def __init__(self, settings):
        self.settings = settings
        settings.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.execute("SELECT id FROM queue_lock WHERE id=1")
            if db.fetchone() is None:
                raise RuntimeError("Nailgun MariaDB schema has not been provisioned")

    @contextmanager
    def db(self, *, write=False):
        settings = self.settings
        db = pymysql.connect(unix_socket=str(settings.mariadb_socket),
                             user=settings.mariadb_user, database=settings.mariadb_database,
                             password=settings.mariadb_password_file.read_text().strip(),
                             charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
                             connect_timeout=5, read_timeout=15, write_timeout=15)
        try:
            with db.cursor() as cursor:
                if write:
                    # Queue claims, limits and idempotency decisions share one transaction lock.
                    cursor.execute("SELECT id FROM queue_lock WHERE id=1 FOR UPDATE")
                    cursor.fetchone()
                yield cursor
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def domains(self):
        with self.db() as db:
            db.execute("SELECT * FROM domains ORDER BY name")
            return list(db.fetchall())

    def ping(self):
        with self.db() as db:
            db.execute("SELECT 1 FROM queue_lock WHERE id=1")
            return db.fetchone() is not None

    def reconcile(self, names):
        # No automatic Google enablement: discovery is not ownership verification.
        with self.db(write=True) as db:
            db.execute("UPDATE domains SET present=0")
            for name in names:
                db.execute(
                    """INSERT INTO domains(name,updated_at) VALUES(%s,%s)
                    ON DUPLICATE KEY UPDATE present=1, updated_at=VALUES(updated_at)""",
                    (name, time.time()),
                )

    def update_domain(self, name, update):
        if update.enabled and not update.google_verified:
            raise ServiceError(422, "Verify the domain in Google before enabling it")
        with self.db(write=True) as db:
            db.execute("SELECT * FROM domains WHERE name=%s", (name,))
            if not db.fetchone():
                raise ServiceError(404, "Domain is not in the server inventory")
            db.execute(
                "UPDATE domains SET enabled=%s,google_verified=%s,notes=%s,updated_at=%s WHERE name=%s",
                (update.enabled, update.google_verified, update.notes, time.time(), name),
            )
        return next(d for d in self.domains() if d["name"] == name)

    def enqueue(self, message: Message | RawMessage, key=None):
        if key is not None and (not key or len(key) > 200):
            raise ServiceError(422, "Idempotency key must contain 1–200 characters")
        payload = message.model_dump_json(by_alias=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        now = time.time()
        with self.db(write=True) as db:
            if key:
                db.execute(f"SELECT {PUBLIC_MESSAGE_COLUMNS}, digest FROM messages WHERE idempotency_key=%s", (key,))
                old = db.fetchone()
                if old:
                    if old["digest"] != digest:
                        raise ServiceError(
                            409, "Idempotency key already used with different content"
                        )
                    return self.public(old)
            db.execute("SELECT * FROM domains WHERE name=%s", (message.domain,))
            domain = db.fetchone()
            if not domain or not all(domain[k] for k in ("enabled", "google_verified", "present")):
                raise ServiceError(
                    403, "Sender domain is absent, disabled, or awaiting Google verification"
                )
            db.execute("SELECT COUNT(*) AS amount FROM messages WHERE status IN ('queued','sending')")
            pending = db.fetchone()["amount"]
            if pending >= self.settings.max_pending:
                raise ServiceError(429, "Queue is full")
            db.execute("SELECT COALESCE(SUM(OCTET_LENGTH(payload)),0) AS amount FROM messages")
            storage = db.fetchone()["amount"]
            if storage + len(payload.encode()) > self.settings.max_storage_bytes:
                raise ServiceError(429, "Message storage limit reached")
            db.execute(
                "SELECT COALESCE(SUM(recipients),0) AS amount FROM messages WHERE created_at>%s AND status!='cancelled'",
                (now - 86400,),
            )
            used = db.fetchone()["amount"]
            if used + len(message.recipients) > self.settings.daily_recipient_limit:
                raise ServiceError(429, "Rolling daily recipient limit reached")
            identifier = str(uuid.uuid4())
            mid = f"<{identifier}@{message.domain}>"
            db.execute(
                """INSERT INTO messages
              (id,message_id,payload,digest,idempotency_key,domain,recipients,status,created_at,updated_at,next_attempt)
              VALUES(%s,%s,%s,%s,%s,%s,%s,'queued',%s,%s,%s)""",
                (
                    identifier,
                    mid,
                    payload,
                    digest,
                    key,
                    message.domain,
                    len(message.recipients),
                    now,
                    now,
                    now,
                ),
            )
            db.execute(f"SELECT {PUBLIC_MESSAGE_COLUMNS} FROM messages WHERE id=%s", (identifier,))
            return self.public(db.fetchone())

    @staticmethod
    def public(row):
        result = {
            k: row[k]
            for k in (
                "id",
                "message_id",
                "domain",
                "status",
                "attempts",
                "created_at",
                "updated_at",
                "next_attempt",
                "error",
            )
        }
        result["accepted_recipients"] = json.loads(row["accepted"] or "[]")
        return result

    def get(self, identifier):
        with self.db() as db:
            db.execute(f"SELECT {PUBLIC_MESSAGE_COLUMNS} FROM messages WHERE id=%s", (identifier,))
            row = db.fetchone()
            if not row:
                raise ServiceError(404, "Message not found")
            return self.public(row)

    def list(self, limit=50):
        if not 1 <= limit <= 100:
            raise ServiceError(422, "Limit must be 1–100")
        with self.db() as db:
            db.execute(f"SELECT {PUBLIC_MESSAGE_COLUMNS} FROM messages ORDER BY created_at DESC LIMIT %s", (limit,))
            return [self.public(row) for row in db.fetchall()]

    def cancel(self, identifier):
        with self.db(write=True) as db:
            changed = db.execute(
                "UPDATE messages SET status='cancelled',updated_at=%s WHERE id=%s AND status='queued'",
                (time.time(), identifier),
            )
        if not changed:
            self.get(identifier)
            raise ServiceError(409, "Only queued messages can be cancelled")
        return self.get(identifier)

    def recover(self):
        with self.db(write=True) as db:
            db.execute(
                "UPDATE messages SET status='uncertain',error='Worker stopped during SMTP delivery; inspect before resending',updated_at=%s WHERE status='sending'",
                (time.time(),),
            )

    def prune_bodies(self):
        with self.db(write=True) as db:
            db.execute(
                "UPDATE messages SET payload='{}' WHERE status IN ('sent','failed','uncertain','cancelled') AND updated_at<%s AND payload!='{}'",
                (time.time() - self.settings.body_retention_days * 86400,),
            )

    def claim(self):
        with self.db(write=True) as db:
            db.execute(
                "SELECT m.* FROM messages m JOIN domains d ON d.name=m.domain WHERE m.status='queued' AND m.next_attempt<=%s AND d.enabled=1 AND d.google_verified=1 AND d.present=1 ORDER BY m.created_at LIMIT 1",
                (time.time(),),
            )
            row = db.fetchone()
            if row:
                db.execute(
                    "UPDATE messages SET status='sending',attempts=attempts+1,updated_at=%s WHERE id=%s",
                    (time.time(), row["id"]),
                )
                db.execute("SELECT * FROM messages WHERE id=%s", (row["id"],))
                return db.fetchone()

    def finish(self, identifier, status, error=None, accepted=None, delay=0):
        with self.db(write=True) as db:
            db.execute(
                "UPDATE messages SET status=%s,error=%s,accepted=%s,updated_at=%s,next_attempt=%s "
                "WHERE id=%s AND status='sending'",
                (
                    status,
                    error,
                    json.dumps(accepted or []),
                    time.time(),
                    time.time() + delay,
                    identifier,
                ),
            )
