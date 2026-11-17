#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
"""Credentials of the Company job roles (company-design 10, O7).

    db-jobs.py ensure <password-file> <port>

Keeps one libpq password file (0600, in the 0700 Company config directory,
owned by the Company account) with a random password for each job login role
(oaos_company_backup, oaos_company_maint) on 127.0.0.1:<port>, any database of
the local cluster (the C17 recovery drill reads its scratch database). Missing
entries are generated; existing ones are kept.

Standard output receives the SQL that sets each role's password as a SCRAM-
SHA-256 verifier computed here, for the caller to pipe into the maintenance
account's psql. The password itself never reaches a command line, the server
log or standard error; nothing is printed when the file is invalid.
"""

import base64
import hashlib
import hmac
import os
import secrets
import stat
import sys
import tempfile

ROLES = ("oaos_company_backup", "oaos_company_maint")
HOST = "127.0.0.1"
ITERATIONS = 4096


class JobCredentialError(Exception):
    """The password file or its directory is unsafe or malformed."""


def _check_directory(path):
    directory = os.path.dirname(path)
    info = os.stat(directory)
    if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise JobCredentialError("credential directory permissions are invalid")


def read_entries(path, port):
    """Return {role: password} for this host/port; validates mode and owner."""
    _check_directory(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()):
            raise JobCredentialError("credential file permissions are invalid")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as source:
            lines = source.read().splitlines()
    finally:
        os.close(fd)
    entries = {}
    for line in lines:
        parts = line.split(":")
        if len(parts) != 5 or parts[0] != HOST or parts[1] != str(port) or parts[2] != "*":
            continue
        if parts[3] in ROLES and parts[4]:
            if parts[3] in entries:
                raise JobCredentialError("credential entry is duplicated")
            entries[parts[3]] = parts[4]
    return entries


def write_entries(path, port, entries):
    _check_directory(path)
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".pgpass-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            for role in ROLES:
                target.write(":".join((HOST, str(port), "*", role, entries[role])) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def scram_verifier(password, salt=None, iterations=ITERATIONS):
    """PostgreSQL SCRAM-SHA-256 verifier (RFC 5802/7677) of an ASCII password."""
    salt = salt if salt is not None else os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    encode = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return "SCRAM-SHA-256$%d:%s$%s:%s" % (
        iterations, encode(salt), encode(stored_key), encode(server_key))


def ensure(path, port):
    entries = read_entries(path, port)
    if any(role not in entries for role in ROLES):
        # token_hex keeps the password free of ':' and '\' (libpq file syntax).
        entries = {role: entries.get(role) or secrets.token_hex(32) for role in ROLES}
        write_entries(path, port, entries)
    statements = []
    for role in ROLES:
        statements.append("ALTER ROLE %s PASSWORD '%s';" % (role, scram_verifier(entries[role])))
    return "\n".join(statements) + "\n"


def main(argv):
    if len(argv) != 4 or argv[1] != "ensure" or not argv[3].isdigit():
        return 2
    try:
        sys.stdout.write(ensure(argv[2], int(argv[3])))
    except (OSError, JobCredentialError):
        print("Company job credential file is unsafe or unreadable.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
