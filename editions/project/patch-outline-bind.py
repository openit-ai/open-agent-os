#!/usr/bin/env python3
"""Apply the approved v1.10.1 loopback bind change to an Outline checkout."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

OLD = '  server.listen(normalizedPort);'
NEW = '  server.listen(normalizedPort, "127.0.0.1");'


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


def replace_atomic(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".main-oaos-", delete=False
    ) as temporary:
        temporary.write(content)
        os.fchmod(temporary.fileno(), mode)
        name = temporary.name
    os.replace(name, path)


def main(root: Path) -> int:
    path = root / "server/main.ts"
    original = path.read_text(encoding="utf-8")
    old_count = original.splitlines().count(OLD)
    new_count = original.splitlines().count(NEW)
    if old_count == 1 and new_count == 0:
        if git(root, "diff", "--name-only"):
            print("Outline source already has changes; patch was not applied.", file=sys.stderr)
            return 3
        replace_atomic(path, original.replace(OLD, NEW, 1))
        applied = True
    elif old_count == 0 and new_count == 1:
        applied = False
    else:
        print("Outline bind patch precondition failed; source left unchanged.", file=sys.stderr)
        return 3

    if (
        git(root, "diff", "--numstat", "--", "server/main.ts")
        != "1\t1\tserver/main.ts"
        or git(root, "diff", "--name-only") != "server/main.ts"
    ):
        if applied:
            replace_atomic(path, original)
        print("Outline source differs by more than the approved one-line patch.", file=sys.stderr)
        return 3
    print("Outline loopback bind patch verified.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: patch-outline-bind.py <outline-checkout>")
    raise SystemExit(main(Path(sys.argv[1])))
