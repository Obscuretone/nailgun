"""Compatibility adapter for existing website event notifications, using VPS Mail."""

import argparse
import json
import re
import subprocess
import sys
from email.headerregistry import Address
from email.message import EmailMessage
from pathlib import Path

CONFIG = Path("/etc/site-alert-smtp.json")


def send_alert(config, site, reference, subject, body):
    sender = Address(config["sites"][site]["name"], addr_spec=config["sites"][site]["from"])
    message = EmailMessage()
    message["From"] = sender
    message["To"] = Address(addr_spec=config["recipient"])
    message["Subject"] = subject
    message["Message-ID"] = f"<{reference}@{sender.domain}>"
    message.set_content(body, cte="quoted-printable")
    # Keep event references stable so upstream retries reuse the queue record.
    subprocess.run(
        ["/usr/local/sbin/vps-sendmail", "-t", "-i", "-f", sender.addr_spec],
        input=message.as_bytes(),
        check=True,
        timeout=40,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site")
    parser.add_argument("reference")
    parser.add_argument("subject")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{1,80}", args.reference):
        parser.error("Invalid alert reference")
    body = sys.stdin.read(16385)
    if len(body) > 16384:
        parser.error("Alert text exceeds the length limit")
    try:
        send_alert(json.loads(CONFIG.read_text()), args.site, args.reference, args.subject, body)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        sys.exit("VPS Mail did not acknowledge the alert submission.")


if __name__ == "__main__":
    main()
