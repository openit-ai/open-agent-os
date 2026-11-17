#!/usr/bin/env python3
# SPDX-License-Identifier: BUSL-1.1
# Company edition: see LICENSE-COMPANY at the repository root.
"""Phase evidence JSON for one Company read-back run (company-design 9, O8).

    evidence.py <phase> <exit code> <started_at> <finished_at> <stdout file>

Writes ~/.oaos-install/evidence/company-<phase>-<UTC timestamp>.json (directory
0700, file 0600) with the phase, the non-secret E identifier, UTC timestamps,
the read-back command and exit code, every printed check row (name/status),
the MANUAL items, the latest rollback snapshot of the phase (name only), the
final checkpoint state, and the read-back output after redaction with its
SHA-256. Secrets never enter the file: values of secret-like keys in the
Company EnvironmentFile and the job password file, plus anything that looks
like a key, token or long credential, are replaced before hashing. Prints the
evidence path only.
"""

import datetime
import hashlib
import json
import os
import re
import sys

STATUS = ("PASS", "FAIL", "MANUAL", "SKIP")
ROW = re.compile(r"^(?:\d+\s+)?(?P<name>\S.*?)\s+(?P<status>PASS|FAIL|MANUAL|SKIP)(?:\s+(?P<detail>.*))?$")
SUMMARY = re.compile(r"^Summary: PASS=(\d+) FAIL=(\d+) MANUAL=(\d+) SKIP=(\d+)$")
SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|SIGNING|COOKIE|PGPASS)", re.I)
PATTERNS = (
    # name=value / name: value for credential-like names
    (re.compile(r"(?i)\b([A-Za-z0-9_]*(?:key|secret|token|password|passwd|credential)[A-Za-z0-9_]*)"
                r"(\s*[=:]\s*)\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer [REDACTED]"),
    (re.compile(r"\b[0-9a-fA-F]{40,}\b"), "[REDACTED]"),
    (re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}"), "[REDACTED]"),
    (re.compile(r"SCRAM-SHA-256\$\S+"), "[REDACTED]"),
)


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as source:
            return source.read()
    except OSError:
        return ""


def secret_values(home):
    """Values that must never appear in evidence (EnvironmentFile, job file)."""
    values = set()
    config = os.path.join(home, ".config", "oaos-company")
    for line in _read(os.path.join(config, ".env")).splitlines():
        name, sep, value = line.partition("=")
        if sep and SECRET_NAME.search(name) and len(value.strip()) >= 6:
            values.add(value.strip())
    for line in _read(os.path.join(config, "db-jobs.pgpass")).splitlines():
        parts = line.split(":")
        if len(parts) == 5 and len(parts[4]) >= 6:
            values.add(parts[4])
    for name, value in os.environ.items():
        if SECRET_NAME.search(name) and len(value) >= 6:
            values.add(value)
    return sorted(values, key=len, reverse=True)


def redact(text, values):
    for value in values:
        text = text.replace(value, "[REDACTED]")
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def environment_id(home):
    for line in _read(os.path.join(home, ".config", "oaos-company", "deployment.conf")).splitlines():
        if line.startswith("OAOS_COMPANY_E_ID="):
            value = line.split("=", 1)[1].strip()
            if re.match(r"^E-[A-Za-z0-9_-]{1,64}$", value):
                return value
    value = os.environ.get("OAOS_COMPANY_E_ID", "")
    return value if re.match(r"^E-[A-Za-z0-9_-]{1,64}$", value) else "unknown"


def final_state(home, phase):
    try:
        with open(os.path.join(home, ".oaos-install", "company-state.json"), encoding="utf-8") as src:
            state = json.load(src)
        return str(state.get("stages", {}).get(phase, {}).get("status", "pending"))
    except (OSError, ValueError, AttributeError):
        return "unknown"


def rollback_reference(phase):
    directory = os.environ.get("OAOS_COMPANY_ROLLBACK_DIR", "")
    if not directory or not os.path.isdir(directory):
        return None
    names = sorted(name for name in os.listdir(directory)
                   if re.match(r"^%s-\d{8}T\d{6}Z\.[A-Za-z0-9]+$" % re.escape(phase), name))
    return names[-1] if names else None


def build(phase, code, started, finished, output, home):
    values = secret_values(home)
    redacted = redact(output, values)
    checks, manual, summary = [], [], None
    for line in redacted.splitlines():
        line = line.rstrip()
        match = SUMMARY.match(line)
        if match:
            summary = dict(zip(("pass", "fail", "manual", "skip"), map(int, match.groups())))
            continue
        match = ROW.match(line)
        if not match or line.startswith(("CHECK ", "Summary")):
            continue
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        checks.append({"name": name, "status": match.group("status")})
        if match.group("status") == "MANUAL":
            manual.append({"name": name, "status": "pending",
                           "owner_role": "E operator"})
    reference = rollback_reference(phase)
    return {
        "schema": "oaos-company-evidence/1",
        "phase": phase,
        "environment": environment_id(home),
        "started_at": started,
        "finished_at": finished,
        "verify": {
            "command": "bash bootstrap/verify/company-verify.sh --phase %s --read-back" % phase,
            "exit_code": code,
        },
        "checks": checks,
        "summary": summary,
        "manual": manual,
        "rollback": {"performed": False,
                     "reference": reference,
                     "result": "snapshot_available" if reference else "not_required"},
        "final_state": final_state(home, phase),
        "stdout": {"redacted": redacted,
                   "sha256": hashlib.sha256(redacted.encode("utf-8")).hexdigest()},
    }


def write(document, home, phase):
    directory = os.path.join(home, ".oaos-install", "evidence")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.join(directory, "company-%s-%s" % (phase, stamp))
    path, suffix = base + ".json", 1
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            break
        except FileExistsError:
            suffix += 1
            path = "%s-%d.json" % (base, suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        json.dump(document, target, ensure_ascii=False, indent=1, sort_keys=True)
        target.write("\n")
    os.chmod(path, 0o600)
    return path


def main(argv):
    if len(argv) != 6 or not re.match(r"^c(0[1-9]|1[0-7])$", argv[1]) or not argv[2].isdigit():
        return 2
    home = os.environ.get("HOME", "")
    if not home:
        return 2
    document = build(argv[1], int(argv[2]), argv[3], argv[4], _read(argv[5]), home)
    print(write(document, home, argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
