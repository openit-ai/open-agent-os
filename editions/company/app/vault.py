"""In-process AES-256-GCM envelope for Company personal credentials.

Callers obtain the key from the restricted Company EnvironmentFile, authenticate
the Project session, and pass the encrypted envelope to the C05 SQL functions.
This module has no plaintext-printing command-line interface.
"""

import base64
import os
import stat
import sys

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


KEY_NAME = "OAOS_COMPANY_VAULT_KEY_V1"


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


def key_from_environment(version=1):
    if not isinstance(version, int) or version < 1:
        raise VaultAccessDenied("vault key version is invalid")
    return _key(os.environ.get("OAOS_COMPANY_VAULT_KEY_V" + str(version), ""))


def _aad(org_id, owner_id, provider, purpose):
    parts = (str(org_id), str(owner_id), provider, purpose)
    if any(not part or "\0" in part for part in parts):
        raise VaultAccessDenied("vault binding is invalid")
    return b"\0".join(part.encode("utf-8") for part in parts)


def seal(plaintext, key, org_id, owner_id, provider, purpose):
    if not isinstance(plaintext, bytes) or not plaintext:
        raise VaultAccessDenied("credential is invalid")
    if len(key) != 32:
        raise VaultAccessDenied("vault key is invalid")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(
        nonce, plaintext, _aad(org_id, owner_id, provider, purpose)
    )
    return ciphertext, nonce


def open_envelope(ciphertext, nonce, key, org_id, owner_id, provider, purpose):
    if len(key) != 32 or len(nonce) != 12:
        raise VaultAccessDenied("credential is unavailable")
    try:
        return AESGCM(key).decrypt(
            nonce, ciphertext, _aad(org_id, owner_id, provider, purpose)
        )
    except (InvalidTag, ValueError) as exc:
        raise VaultAccessDenied("credential is unavailable") from exc


def ensure_key(env_path):
    """Create one key in an existing 0600 EnvironmentFile; never print it."""
    directory = os.path.dirname(env_path)
    if stat.S_IMODE(os.stat(directory).st_mode) != 0o700:
        raise VaultAccessDenied("vault directory permissions are invalid")
    flags = os.O_RDWR | os.O_NOFOLLOW
    fd = os.open(env_path, flags)
    try:
        mode = os.fstat(fd)
        if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o600:
            raise VaultAccessDenied("vault file permissions are invalid")
        if mode.st_uid != os.getuid():
            raise VaultAccessDenied("vault file ownership is invalid")
        with os.fdopen(fd, "r+", encoding="utf-8", closefd=False) as env_file:
            entries = [line.split("=", 1)[1].strip()
                       for line in env_file if line.startswith(KEY_NAME + "=")]
            if len(entries) > 1:
                raise VaultAccessDenied("vault key is duplicated")
            if entries:
                _key(entries[0])
                return
            env_file.seek(0, os.SEEK_END)
            if env_file.tell():
                env_file.seek(env_file.tell() - 1)
                if env_file.read(1) != "\n":
                    env_file.write("\n")
            env_file.seek(0, os.SEEK_END)
            env_file.write(KEY_NAME + "=" + base64.b64encode(os.urandom(32)).decode("ascii") + "\n")
            env_file.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def check_key(env_path):
    directory = os.path.dirname(env_path)
    if stat.S_IMODE(os.stat(directory).st_mode) != 0o700:
        raise VaultAccessDenied("vault directory permissions are invalid")
    fd = os.open(env_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        mode = os.fstat(fd)
        if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o600:
            raise VaultAccessDenied("vault file permissions are invalid")
        if mode.st_uid != os.getuid():
            raise VaultAccessDenied("vault file ownership is invalid")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as env_file:
            entries = [line.split("=", 1)[1].strip()
                       for line in env_file if line.startswith(KEY_NAME + "=")]
        if len(entries) != 1:
            raise VaultAccessDenied("vault key is missing or duplicated")
        _key(entries[0])
    finally:
        os.close(fd)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("ensure-key", "check-key"):
        sys.exit(1)
    try:
        if sys.argv[1] == "ensure-key":
            ensure_key(sys.argv[2])
        else:
            check_key(sys.argv[2])
    except (OSError, VaultAccessDenied):
        print("C05 vault key setup failed.", file=sys.stderr)
        sys.exit(3)
