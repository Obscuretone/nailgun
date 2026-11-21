"""Run as root from a systemd timer; discovery never enables mail automatically."""

import re
import subprocess
import sys

from app.config import Settings
from app.store import Store


def inventory(config: str) -> list[str]:
    names = set()
    for match in re.finditer(r"^\s*server_name\s+([^;]+);", config, re.MULTILINE):
        for name in match.group(1).split():
            name = name.lower().rstrip(".")
            name = name.removeprefix("www.")
            if re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}", name):
                names.add(name)
    return sorted(names)


if __name__ == "__main__":
    if "--stdin" in sys.argv:
        config = sys.stdin.read()
    else:
        config = subprocess.run(
            ["/usr/sbin/nginx", "-T"], capture_output=True, text=True, check=True
        ).stdout
    settings = Settings()
    store = Store(settings)
    names = inventory(config)
    if not names:
        raise SystemExit("Empty nginx domain inventory; refusing to clear existing inventory")
    names = sorted(set(names) | set(settings.additional_domains))
    store.reconcile(names)
    store.prune_bodies()
    pending = [d["name"] for d in store.domains() if d["present"] and not d["enabled"]]
    print(f"Synchronized {len(names)} nginx domains. Pending setup: {', '.join(pending) or 'none'}")
