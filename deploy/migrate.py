import os
import secrets
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pymysql


DATABASES = {"nailgun", "nailgun_status", "vpsops_nailgun_check"}
TABLES = {
    "domains": ("name", "enabled", "google_verified", "present", "notes", "updated_at"),
    "messages": ("id", "message_id", "payload", "digest", "idempotency_key", "domain", "recipients",
                 "status", "attempts", "created_at", "updated_at", "next_attempt", "error", "accepted"),
}
SOCKET = "/run/mysqld/mysqld.sock"


def provision(schema, credential, name):
    with pymysql.connect(user="root", unix_socket=SOCKET, autocommit=True) as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT User FROM mysql.user WHERE User=%s AND Host='localhost'", (name,))
            existing = cursor.fetchone() is not None
            if credential.is_file():
                password = credential.read_text().strip()
            elif not existing:
                password = secrets.token_urlsafe(32)
                descriptor = os.open(credential, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as target:
                    target.write(password + "\n")
            else:
                raise RuntimeError("Existing MariaDB user needs its matching credential")
            if not password:
                raise RuntimeError("Nailgun MariaDB credential is empty")
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_nopad_bin")
            cursor.execute(f"USE `{name}`")
            for statement in schema.read_text().split(";"):
                if statement.strip():
                    cursor.execute(statement)
            if not existing:
                cursor.execute("CREATE USER %s@'localhost' IDENTIFIED BY %s", (name, password))
            cursor.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{name}`.* TO %s@'localhost'", (name,))
    with pymysql.connect(user=name, password=password, database=name, unix_socket=SOCKET) as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT id FROM queue_lock WHERE id=1")
            if cursor.fetchone() != (1,):
                raise RuntimeError("Nailgun queue lock could not be verified")


def migrate(source, name):
    with (
        closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as original,
        pymysql.connect(user="root", unix_socket=SOCKET, database=name, charset="utf8mb4") as replacement,
    ):
        if original.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("SQLite queue integrity check failed")
        with replacement.cursor() as cursor:
            for table, columns in TABLES.items():
                fields = ", ".join(f"`{column}`" for column in columns)
                select = f"SELECT {fields} FROM `{table}` ORDER BY `{columns[0]}`"
                initial = tuple(original.execute(select))
                cursor.execute(select)
                destination = cursor.fetchall()
                if destination and destination != initial:
                    raise RuntimeError(f"MariaDB already contains different {table} rows")
                if not destination:
                    values = ", ".join("%s" for _ in columns)
                    for row in initial:
                        cursor.execute(f"INSERT INTO `{table}` ({fields}) VALUES ({values})", row)
                cursor.execute(select)
                if cursor.fetchall() != initial:
                    raise RuntimeError(f"Nailgun {table} failed row reconciliation")
                print(name, table, len(initial))
        replacement.commit()


if __name__ == "__main__":
    method, database, *parameters = sys.argv[1:]
    if database not in DATABASES:
        raise ValueError("Unknown Nailgun database")
    if method == "provision":
        schema, credential = map(Path, parameters)
        provision(schema, credential, database)
    elif method == "import":
        source, = map(Path, parameters)
        migrate(source, database)
    else:
        raise ValueError("Use provision or import")
