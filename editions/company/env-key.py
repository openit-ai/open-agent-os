#!/usr/bin/env python3
"""Atomically store one provider key in the Company EnvironmentFile."""
from __future__ import annotations

import os
import sys
import tempfile


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    path, name = sys.argv[1], sys.argv[2]
    if not name or not name.replace("_", "").isalnum() or not name.isupper():
        return 2
    secret = sys.stdin.read().strip()
    if not secret or any(char in secret for char in "\r\n"):
        return 2
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as source:
            lines = source.read().splitlines()
    lines = [line for line in lines if not line.startswith(name + "=")]
    lines.append(name + "=" + secret)
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".env-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
