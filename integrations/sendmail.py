#!/usr/bin/python3
"""A sendmail submission client. Exit zero means durably queued, not delivered."""

import base64
import json
import os
import socket
import struct
import sys
import uuid
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses

SOCKET = "/run/vps-mail-local/mail.sock"
MAX_BYTES = 5 * 1024 * 1024


def arguments(args):
    recipients = []
    sender = None
    read_headers = False
    check = False
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            recipients.extend(args[index:])
            break
        if arg in ("-t",):
            read_headers = True
        elif arg in ("-i", "-oi", "-oem", "-odb", "-odi", "-odq", "-bm"):
            pass
        elif arg in ("--check",):
            check = True
        elif arg in ("-f", "-r", "-F"):
            if index == len(args):
                raise ValueError("Missing option value")
            if arg != "-F":
                sender = args[index]
            index += 1
        elif arg.startswith(("-f", "-r")) and len(arg) > 2:
            sender = arg[2:]
        elif arg.startswith("-F") and len(arg) > 2:
            pass
        elif arg.startswith("-"):
            raise ValueError("Unsupported sendmail option: " + arg)
        else:
            recipients.append(arg)
    return recipients, sender, read_headers, check


def request(raw, args):
    recipients, sender, read_headers, check = arguments(args)
    if len(raw) > MAX_BYTES:
        raise ValueError("MIME message exceeds 5 MiB")
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    if read_headers:
        recipients.extend(str(v) for k in ("To", "Cc", "Bcc") for v in mail.get_all(k, []))
    recipients = list(dict.fromkeys(address for _, address in getaddresses(recipients)))
    if not recipients:
        raise ValueError("No recipients; supply addresses or -t with To/Cc/Bcc headers")
    if "Message-ID" not in mail:
        mail["Message-ID"] = "<" + str(uuid.uuid4()) + "@local.vps-mail>"
    return {
        "raw": base64.b64encode(mail.as_bytes(policy=policy.SMTP)).decode(),
        "recipients": recipients,
        "sender": sender,
        "check": check,
    }


def read_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise OSError("Local mail service closed the connection")
        data.extend(chunk)
    return bytes(data)


def submit(data, path=SOCKET):
    payload = json.dumps(data).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(40)
        connection.connect(path)
        connection.sendall(struct.pack("!I", len(payload)) + payload)
        length = struct.unpack("!I", read_exact(connection, 4))[0]
        if length > 65536:
            raise OSError("Invalid local mail response")
        return json.loads(read_exact(connection, length))


def main():
    try:
        data = request(sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[1:])
        result = submit(data, os.environ.get("NAILGUN_SENDMAIL_SOCKET", SOCKET))
        if not result.get("ok"):
            print("vps-sendmail: " + result.get("error", "Submission failed"), file=sys.stderr)
            return 75 if result.get("status_code", 503) in (429, 503) else 65
        if data["check"]:
            print(json.dumps(result))
        return 0
    except ValueError as exc:
        print("vps-sendmail: " + str(exc), file=sys.stderr)
        return 64
    except OSError:
        print(
            "vps-sendmail: local mail service unavailable; message was not acknowledged",
            file=sys.stderr,
        )
        return 75


if __name__ == "__main__":
    sys.exit(main())
