# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
"""In-process AES-256-GCM envelope for Company personal credentials.

Callers obtain the key from the restricted Company EnvironmentFile, authenticate
the Project session, and pass the encrypted envelope to the C05 SQL functions.
This module has no plaintext-printing command-line interface.

Key store (company-design 10, O5). The EnvironmentFile holds one or more
OAOS_COMPANY_VAULT_KEY_V<n> entries and OAOS_COMPANY_VAULT_KEY_CURRENT=<n>
(absent means 1, the layout before rotation existed). New envelopes use the
current version and bind it in the AES-GCM additional data together with the
organization, owner, provider and purpose. Version-1 envelopes written before
that change carry the legacy additional data without the version; they still
open with the version-1 key. `rotate` re-encrypts every active credential to a
new version through the maintenance login and retires the old key only after
the database reads back zero active rows on it.
"""

import base64
import datetime
import hmac
import os
import re
import stat
import subprocess
import sys
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


KEY_PREFIX = "OAOS_COMPANY_VAULT_KEY_V"
KEY_NAME = KEY_PREFIX + "1"
CURRENT_NAME = "OAOS_COMPANY_VAULT_KEY_CURRENT"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_B64 = re.compile(r"^[A-Za-z0-9+/=]*$")


class VaultAccessDenied(Exception):
    """Credential is unavailable or its authenticated envelope is invalid."""


def _key(raw):
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise VaultAccessDenied("vault key is invalid") from exc
    if len(key) != 32:
        raise VaultAccessDenied("vault key is invalid")
    return key


def _version(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise VaultAccessDenied("vault key version is invalid")
    return value


def key_from_environment(version=None):
    """Key of `version`, or of the current version, from the process environment."""
    if version is None:
        raw = os.environ.get(CURRENT_NAME, "1")
        if not raw.isdigit():
            raise VaultAccessDenied("vault key version is invalid")
        version = int(raw)
    _version(version)
    return _key(os.environ.get(KEY_PREFIX + str(version), ""))


def _aad(org_id, owner_id, provider, purpose, key_version=None):
    parts = [str(org_id), str(owner_id), provider, purpose]
    if any(not part or "\0" in part for part in parts):
        raise VaultAccessDenied("vault binding is invalid")
    if key_version is not None:
        parts.append("key_version=%d" % _version(key_version))
    return b"\0".join(part.encode("utf-8") for part in parts)


def seal(plaintext, key, org_id, owner_id, provider, purpose, key_version=1):
    """Encrypt with a fresh nonce; the key version is bound in the AAD."""
    if not isinstance(plaintext, bytes) or not plaintext:
        raise VaultAccessDenied("credential is invalid")
    if len(key) != 32:
        raise VaultAccessDenied("vault key is invalid")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(
        nonce, plaintext, _aad(org_id, owner_id, provider, purpose, key_version)
    )
    return ciphertext, nonce


def open_envelope(ciphertext, nonce, key, org_id, owner_id, provider, purpose,
                  key_version=1):
    """Decrypt an envelope of `key_version`; legacy V1 envelopes still open."""
    if len(key) != 32 or len(nonce) != 12:
        raise VaultAccessDenied("credential is unavailable")
    candidates = [_aad(org_id, owner_id, provider, purpose, key_version)]
    if key_version == 1:
        candidates.append(_aad(org_id, owner_id, provider, purpose))
    for aad in candidates:
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, aad)
        except (InvalidTag, ValueError):
            continue
    raise VaultAccessDenied("credential is unavailable")


def _check_directory(env_path):
    directory = os.path.dirname(env_path) or "."
    if stat.S_IMODE(os.stat(directory).st_mode) != 0o700:
        raise VaultAccessDenied("vault directory permissions are invalid")


def _secure_lines(env_path):
    """Lines of a 0600 file owned by this account in a 0700 directory."""
    _check_directory(env_path)
    fd = os.open(env_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        mode = os.fstat(fd)
        if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o600:
            raise VaultAccessDenied("vault file permissions are invalid")
        if mode.st_uid != os.getuid():
            raise VaultAccessDenied("vault file ownership is invalid")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as env_file:
            return env_file.read().splitlines()
    finally:
        os.close(fd)


def _write_lines(env_path, lines):
    """Atomically replace a 0600 file (same directory, fsync before rename)."""
    _check_directory(env_path)
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(env_path), prefix=".env-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write("\n".join(lines) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, env_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _parse_store(lines):
    keys = {}
    current = None
    for line in lines:
        name, sep, value = line.partition("=")
        if not sep:
            continue
        if name == CURRENT_NAME:
            if current is not None or not value.strip().isdigit():
                raise VaultAccessDenied("vault current key version is invalid")
            current = int(value.strip())
            continue
        if not name.startswith(KEY_PREFIX):
            continue
        suffix = name[len(KEY_PREFIX):]
        if not suffix.isdigit() or int(suffix) < 1 or str(int(suffix)) != suffix:
            raise VaultAccessDenied("vault key name is invalid")
        version = int(suffix)
        if version in keys:
            raise VaultAccessDenied("vault key is duplicated")
        keys[version] = _key(value.strip())
    return current, keys


def load_keys(env_path):
    """Every OAOS_COMPANY_VAULT_KEY_V<n> of the file as {n: 32-byte key}."""
    return _parse_store(_secure_lines(env_path))[1]


def load_store(env_path):
    """(current version, {version: key}); the current key must be present."""
    current, keys = _parse_store(_secure_lines(env_path))
    current = 1 if current is None else current
    if current not in keys:
        raise VaultAccessDenied("vault current key is missing")
    return current, keys


def ensure_key(env_path):
    """Create the first key in an existing 0600 EnvironmentFile; never print it.

    A file that already holds any vault key (including one rotated past V1) is
    only validated, so a re-run never brings a retired key version back.
    """
    lines = _secure_lines(env_path)
    current, keys = _parse_store(lines)
    if keys:
        load_store(env_path)
        return
    if current is not None:
        raise VaultAccessDenied("vault current key is missing")
    _write_lines(env_path, lines + [KEY_NAME + "=" + base64.b64encode(os.urandom(32)).decode("ascii")])


def check_key(env_path):
    load_store(env_path)


def check_backup(live_env, restored_env, database=None):
    """C17 recovery drill: the restored key backup holds every live key.

    Compares key bytes in constant time and proves the restored current key
    with an AES-GCM canary. With `database` (the restored scratch copy), every
    active credential there must also open with the restored keys. Prints
    nothing about the keys or credentials themselves.
    """
    live = load_keys(live_env)
    current, restored = load_store(restored_env)
    if not live:
        raise VaultAccessDenied("vault key is missing")
    for version, key in live.items():
        if not hmac.compare_digest(restored.get(version, b""), key):
            raise VaultAccessDenied("key backup does not hold every vault key")
    canary = os.urandom(32)
    sealed, nonce = seal(canary, restored[current], "drill", "drill", "drill", "drill",
                         current)
    if open_envelope(sealed, nonce, restored[current], "drill", "drill", "drill",
                     "drill", current) != canary:
        raise VaultAccessDenied("restored vault key failed the canary")
    opened = 0
    if database:
        lines = _secure_lines(live_env)
        after = None
        while True:
            rows = _inventory(lines, 2147483647, 1000, after, database)
            for secret_id, org_id, owner_id, provider, purpose, ciphertext, nonce, version in rows:
                version = int(version)
                if version not in restored:
                    raise VaultAccessDenied("restored keys miss a version in use")
                open_envelope(base64.b64decode(ciphertext), base64.b64decode(nonce),
                              restored[version], org_id, owner_id, provider, purpose, version)
                opened += 1
            if len(rows) < 1000:
                break
            after = rows[-1][0]
    return sorted(live), opened


# --- Maintenance login (O7) ---------------------------------------------------

def _env_value(lines, name, default=None):
    for line in lines:
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return os.environ.get(name, default)


def _maint_psql(env_lines, sql, database=None):
    """Run SQL (standard input) as oaos_company_maint; returns output lines."""
    database = database or _env_value(env_lines, "PGDATABASE")
    port = _env_value(env_lines, "PGPORT", "5432")
    if not database or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", database) or not port.isdigit():
        raise VaultAccessDenied("vault database settings are invalid")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("PG")}
    env.update({
        "PGHOST": "127.0.0.1", "PGHOSTADDR": "127.0.0.1", "PGPORT": port,
        "PGUSER": "oaos_company_maint", "PGDATABASE": database,
        "PGPASSFILE": os.path.join(os.path.expanduser("~"), ".config", "oaos-company",
                                   "db-jobs.pgpass"),
        "PGCONNECT_TIMEOUT": "10",
    })
    result = subprocess.run(
        ["psql", "-X", "-q", "-A", "-t", "-F", "\x1f", "-v", "ON_ERROR_STOP=1",
         "--single-transaction"],
        input=sql, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise VaultAccessDenied("vault maintenance query failed")
    return [line for line in result.stdout.splitlines() if line]


def key_usage(env_lines, database=None):
    """{key_version: active rows} read back through vault_key_usage()."""
    usage = {}
    for line in _maint_psql(env_lines, "SELECT key_version, active_rows FROM company.vault_key_usage();\n",
                            database):
        version, count = line.split("\x1f")
        usage[int(version)] = int(count)
    return usage


def check_usage(env_path):
    """Every key version still used by an active credential is in the file."""
    current, keys = load_store(env_path)
    usage = key_usage(_secure_lines(env_path))
    missing = sorted(v for v, count in usage.items() if count and v not in keys)
    if missing:
        raise VaultAccessDenied("a key version in use is missing")
    return current, usage


def _backup_env(env_path, key_backup_dir, label):
    """0700 snapshot directory with a 0600 copy of the EnvironmentFile."""
    if stat.S_IMODE(os.stat(key_backup_dir).st_mode) != 0o700:
        raise VaultAccessDenied("key backup directory permissions are invalid")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = tempfile.mkdtemp(prefix="vault-%s-%s." % (label, stamp), dir=key_backup_dir)
    os.chmod(snapshot, 0o700)
    _write_lines(os.path.join(snapshot, "company.env"), _secure_lines(env_path))
    return snapshot


def _inventory(env_lines, below, limit, after=None, database=None):
    """Active credentials below a key version, ordered by id, after `after`."""
    if after is not None and not _UUID.match(after):
        raise VaultAccessDenied("vault inventory cursor is invalid")
    query = ("SELECT id, org_id, owner_id, provider, purpose, "
             "translate(encode(ciphertext, 'base64'), E'\\n', ''), "
             "translate(encode(nonce, 'base64'), E'\\n', ''), key_version "
             "FROM company.vault_key_inventory(%d, %d, %s);\n"
             % (below, limit, "NULL" if after is None else "'%s'::uuid" % after))
    rows = []
    for line in _maint_psql(env_lines, query, database):
        fields = line.split("\x1f")
        if len(fields) != 8 or not _UUID.match(fields[0]):
            raise VaultAccessDenied("vault inventory is malformed")
        rows.append(fields)
    return rows


def _rekey_rows(env_path, keys, target, batch):
    lines = _secure_lines(env_path)
    rows = _inventory(lines, target, batch)
    if not rows:
        return 0, 0
    statements = []
    for secret_id, org_id, owner_id, provider, purpose, ciphertext, nonce, version in rows:
        version = int(version)
        if not _UUID.match(secret_id) or version not in keys:
            raise VaultAccessDenied("credential key version is unavailable")
        old_ciphertext = base64.b64decode(ciphertext)
        plaintext = open_envelope(old_ciphertext, base64.b64decode(nonce),
                                  keys[version], org_id, owner_id, provider, purpose, version)
        new_ciphertext, new_nonce = seal(plaintext, keys[target], org_id, owner_id,
                                         provider, purpose, target)
        values = [base64.b64encode(item).decode("ascii")
                  for item in (old_ciphertext, new_ciphertext, new_nonce)]
        if not all(_B64.match(value) for value in values):
            raise VaultAccessDenied("vault envelope encoding failed")
        statements.append(
            "SELECT company.vault_rekey_secret('%s'::uuid, %d, decode('%s', 'base64'), "
            "decode('%s', 'base64'), decode('%s', 'base64'), %d);"
            % (secret_id, version, values[0], values[1], values[2], target))
    # One transaction per batch: every row moves, or none of the batch does.
    results = _maint_psql(lines, "\n".join(statements) + "\n")
    return len(rows), sum(1 for result in results if result == "rekeyed")


def rotate(env_path, key_backup_dir, batch=100, max_batches=100000):
    """Move every active credential to a new key version, then retire the old.

    1. If no rotation is in progress, add key n+1, make it current and back the
       EnvironmentFile up before any row uses the new key.
    2. Re-encrypt the active rows below the current version in batches.
    3. Read back vault_key_usage(); retire (remove) the older keys only when no
       active row uses them, then back the file up again.
    Returns (state, current version, rows moved); state is "retired" or
    "pending" (rows left, e.g. a service still writing with the old key).
    """
    if not 1 <= batch <= 1000:
        raise VaultAccessDenied("batch size is invalid")
    lines = _secure_lines(env_path)
    current, keys = load_store(env_path)
    if max(keys) == current and len(keys) == 1:
        target = current + 1
        lines = [line for line in lines if not line.startswith(CURRENT_NAME + "=")]
        lines.append(KEY_PREFIX + str(target) + "=" + base64.b64encode(os.urandom(32)).decode("ascii"))
        lines.append(CURRENT_NAME + "=" + str(target))
        _write_lines(env_path, lines)
        _backup_env(env_path, key_backup_dir, "rotate")
        current, keys = load_store(env_path)
    elif current != max(keys):
        raise VaultAccessDenied("vault current key is not the newest key")
    target = current
    moved = 0
    for _ in range(max_batches):
        seen, rekeyed = _rekey_rows(env_path, keys, target, batch)
        moved += rekeyed
        if seen == 0:
            break
        if rekeyed == 0:
            raise VaultAccessDenied("vault re-encryption made no progress")
    usage = key_usage(_secure_lines(env_path))
    if any(count for version, count in usage.items() if version < target):
        return "pending", target, moved
    lines = [line for line in _secure_lines(env_path)
             if not (line.startswith(KEY_PREFIX) and line.partition("=")[0] != KEY_PREFIX + str(target))]
    _write_lines(env_path, lines)
    load_store(env_path)
    _backup_env(env_path, key_backup_dir, "retired")
    return "retired", target, moved


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    try:
        if command == "check-backup" and len(argv) in (4, 5):
            versions, opened = check_backup(argv[2], argv[3], argv[4] if len(argv) == 5 else None)
            print("key versions restored: %s; credentials opened: %d"
                  % (",".join(str(v) for v in versions), opened))
            return 0
        if command == "rotate" and len(argv) == 4:
            state, current, moved = rotate(argv[2], argv[3])
            print("vault rotation %s: current key version %d, %d credentials re-encrypted"
                  % (state, current, moved))
            return 0 if state == "retired" else 3
        if command == "check-usage" and len(argv) == 3:
            current, usage = check_usage(argv[2])
            print("current key version %d; active credentials per version: %s" % (
                current, ",".join("%d:%d" % item for item in sorted(usage.items())) or "none"))
            return 0
        if command in ("ensure-key", "check-key") and len(argv) == 3:
            (ensure_key if command == "ensure-key" else check_key)(argv[2])
            return 0
    except (OSError, ValueError, VaultAccessDenied) as exc:
        messages = {
            "check-backup": "Vault key backup restore check failed.",
            "rotate": "Vault key rotation stopped: %s." % exc,
            "check-usage": "Vault key usage check failed: %s." % exc,
        }
        print(messages.get(command, "C05 vault key setup failed."), file=sys.stderr)
        return 3
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
