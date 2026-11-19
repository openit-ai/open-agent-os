#!/usr/bin/env python3
"""OAOS Project portal service (ADR 0002): application, mail check, Mattermost approval.

Installed by editions/project/install.sh (portal stage) as ~/oaos/portal/bin/oaos-portal.py
and run by the systemd user unit oaos-portal.service. Standard library only.

    nginx (portal.<domain>)  /apply, /api/portal/*  ->  127.0.0.1:OAOS_PORTAL_PORT (8790)

Flow: /apply form -> 6-digit code to the applicant's mail (G9 bot mailbox, 10 minutes)
-> request "pending" -> Mattermost DM to each OAOS_PORTAL_ADMINS user with Approve/Reject
buttons -> /api/portal/decide (signed context, decider role re-read with the bot token,
the applicant cannot decide their own request) -> invites (Mattermost team mail or link,
Outline users.invite) and a result mail -> once the applicant has a Mattermost account,
the background sync adds the user ID to MATTERMOST_ALLOWED_USERS in the Hermes .env and
restarts the gateway once per batch.

Secrets (bot token, SMTP password, callback secret, Outline token) are read from the
Hermes .env on every use and never logged. Data: ~/oaos/portal/portal.db (0600, directory
0700). The audit table is append-only (SQLite triggers) and holds no personal data.
"""

import contextlib
import hashlib
import hmac
import html
import http.server
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

SCHEMA_VERSION = 1
CODE_TTL = 600
CODE_ATTEMPTS = 5
UNVERIFIED_PURGE = 86400
MAX_BODY = 16384
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")
MM_ID_RE = re.compile(r"^[a-z0-9]{26}$")
TEST_MODE = os.environ.get("OAOS_TEST_MODE") == "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  name TEXT, email TEXT, org TEXT, purpose TEXT,
  state TEXT NOT NULL CHECK (state IN ('unverified','pending','approved','rejected','expired')),
  code_hash TEXT, code_expires INTEGER, code_attempts INTEGER NOT NULL DEFAULT 0,
  verified_at INTEGER, decided_at INTEGER,
  mattermost_user_id TEXT, allowlisted_at INTEGER,
  invite_mattermost TEXT, invite_outline TEXT,
  purged_at INTEGER
);
CREATE INDEX IF NOT EXISTS requests_state ON requests(state);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL REFERENCES requests(id),
  decider_id TEXT NOT NULL, decision TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
  decided_at INTEGER NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  request_id TEXT, result TEXT NOT NULL, detail TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_append_only_update BEFORE UPDATE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_append_only_delete BEFORE DELETE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TABLE IF NOT EXISTS rate (key TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS rate_key ON rate(key, at);
"""


def now():
    return int(time.time())


# --- configuration ------------------------------------------------------------------------

class Config:
    """Non-secret settings from the unit environment; secrets from the Hermes .env."""

    def __init__(self, environ):
        home = environ.get("HOME", "")
        self.portal_home = environ.get("OAOS_PORTAL_HOME") or os.path.join(home, "oaos", "portal")
        self.hermes_home = environ.get("HERMES_HOME") or os.path.join(home, ".hermes")
        self.env_file = os.path.join(self.hermes_home, ".env")
        self.port = int(environ.get("OAOS_PORTAL_PORT") or 8790)
        self.public_url = (environ.get("OAOS_PORTAL_PUBLIC_URL") or "").rstrip("/")
        self.callback_url = (environ.get("OAOS_PORTAL_CALLBACK_URL") or self.public_url).rstrip("/")
        self.chat_url = (environ.get("OAOS_PORTAL_CHAT_URL") or "").rstrip("/")
        self.note_url = (environ.get("OAOS_PORTAL_NOTE_URL") or "").rstrip("/")
        self.mm_api = (environ.get("OAOS_MATTERMOST_API") or "http://127.0.0.1:8065").rstrip("/")
        self.outline_api = (environ.get("OAOS_OUTLINE_API") or "http://127.0.0.1:3000").rstrip("/")
        self.sync_seconds = max(5, int(environ.get("OAOS_PORTAL_SYNC_SECONDS") or 60))
        self.retention_days = max(1, int(environ.get("OAOS_PORTAL_RETENTION_DAYS") or 30))
        self.ip_limit = int(environ.get("OAOS_PORTAL_IP_LIMIT") or 5)
        self.email_limit = int(environ.get("OAOS_PORTAL_EMAIL_LIMIT") or 3)
        # Test-only overrides: plain SMTP to a local mock and a gateway restart stand-in.
        self.smtp_port = int(environ.get("OAOS_PORTAL_SMTP_PORT") or 587) if TEST_MODE else 587
        self.smtp_plain = TEST_MODE and environ.get("OAOS_PORTAL_SMTP_PLAIN") == "1"
        restart = environ.get("OAOS_PORTAL_RESTART_CMD") if TEST_MODE else None
        self.restart_cmd = restart.split() if restart else ["hermes", "gateway", "restart"]
        self.db_path = os.path.join(self.portal_home, "portal.db")

    def env(self):
        return read_env(self.env_file)

    def apply_open(self):
        value = self.env().get("OAOS_PORTAL_APPLY", os.environ.get("OAOS_PORTAL_APPLY", "on"))
        return value.strip().lower() not in ("off", "0", "false", "no")


def parse_env_value(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return re.sub(r'\\(.)', r'\1', raw[1:-1])
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw


def read_env(path):
    values = {}
    try:
        with open(path, encoding="utf-8") as source:
            for line in source:
                line = line.rstrip("\n")
                match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$", line)
                if match and match.group(1) not in values:
                    values[match.group(1)] = parse_env_value(match.group(2))
    except OSError:
        pass
    return values


def env_put(path, key, value):
    """Atomically replace KEY in the Hermes .env (first definition kept, duplicates dropped)."""
    if not re.match(r"^[A-Z][A-Z0-9_]*$", key) or "\n" in value or "\r" in value:
        raise ValueError("invalid env entry")
    lines, found = [], False
    with contextlib.suppress(FileNotFoundError):
        with open(path, encoding="utf-8") as source:
            for line in source.read().splitlines():
                if re.match(r"^\s*(?:export\s+)?%s=" % key, line):
                    if not found:
                        lines.append("%s=%s" % (key, value))
                        found = True
                    continue
                lines.append(line)
    if not found:
        lines.append("%s=%s" % (key, value))
    tmp = "%s.oaos-portal.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        target.write("\n".join(lines) + "\n")
        target.flush()
        os.fsync(target.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --- storage ------------------------------------------------------------------------------

class Store:
    def __init__(self, path):
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        if not os.path.exists(path):
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        os.chmod(path, 0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def one(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def audit(self, db, actor, action, request_id, result, detail=""):
        db.execute("INSERT INTO audit(at, actor, action, request_id, result, detail) VALUES (?,?,?,?,?,?)",
                   (now(), actor, action, request_id, result, detail[:300]))

    def rate_hit(self, key, limit, window=3600):
        """Records one hit; True while KEY stays within LIMIT hits per WINDOW seconds."""
        with self.tx() as db:
            db.execute("DELETE FROM rate WHERE at < ?", (now() - window,))
            count = db.execute("SELECT COUNT(*) FROM rate WHERE key = ?", (key,)).fetchone()[0]
            if count >= limit:
                return False
            db.execute("INSERT INTO rate(key, at) VALUES (?, ?)", (key, now()))
            return True


# --- outbound: HTTP APIs, mail, gateway ---------------------------------------------------

def api(method, url, token, body=None, timeout=10):
    """Returns (status, parsed JSON or None). Network errors return (0, None)."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        return status, None


class Outbound:
    def __init__(self, config):
        self.config = config
        self.restart_lock = threading.Lock()

    def mm(self, method, path, body=None):
        token = self.config.env().get("MATTERMOST_TOKEN", "")
        if not token:
            return 0, None
        return api(method, self.config.mm_api + "/api/v4" + path, token, body)

    def outline(self, path, body):
        token = self.config.env().get("OUTLINE_API_TOKEN", "")
        if not token:
            return 0, None
        return api("POST", self.config.outline_api + "/api/" + path, token, body)

    def send_mail(self, to, subject, text):
        env = self.config.env()
        sender, password, host = (env.get("OAOS_MAIL_ADDRESS", ""), env.get("OAOS_MAIL_PASSWORD", ""),
                                  env.get("OAOS_MAIL_SMTP_HOST", ""))
        if not (sender and password and host):
            raise RuntimeError("G9 mail settings are absent from the Hermes .env")
        message = EmailMessage()
        message["From"] = "OAOS Project <%s>" % sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        with smtplib.SMTP(host, self.config.smtp_port, timeout=20) as smtp:
            smtp.ehlo()
            if not self.config.smtp_plain:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(sender, password)
            smtp.send_message(message)

    def bot_id(self):
        status, body = self.mm("GET", "/users/me")
        return body.get("id") if status == 200 and isinstance(body, dict) else None

    def dm(self, user_id, message, attachments=None):
        bot = self.bot_id()
        if not bot or not MM_ID_RE.match(user_id):
            return False
        status, channel = self.mm("POST", "/channels/direct", [bot, user_id])
        if status not in (200, 201) or not isinstance(channel, dict):
            return False
        post = {"channel_id": channel.get("id"), "message": message}
        if attachments:
            post["props"] = {"attachments": attachments}
        status, _ = self.mm("POST", "/posts", post)
        return status in (200, 201)

    def restart_gateway(self):
        with self.restart_lock:
            try:
                return subprocess.run(self.config.restart_cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=120, check=False).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False


# --- the portal ---------------------------------------------------------------------------

def sign(secret, request_id, action):
    return hmac.new(secret.encode(), ("decide:%s:%s" % (request_id, action)).encode(), hashlib.sha256).hexdigest()


def code_hash(secret, request_id, code):
    return hmac.new(secret.encode(), ("code:%s:%s" % (request_id, code)).encode(), hashlib.sha256).hexdigest()


class Portal:
    def __init__(self, config, store, outbound):
        self.config, self.store, self.out = config, store, outbound

    def secret(self):
        value = self.config.env().get("OAOS_PORTAL_CALLBACK_SECRET", "")
        if len(value) < 32:
            raise RuntimeError("OAOS_PORTAL_CALLBACK_SECRET is absent or too short")
        return value

    def admins(self):
        raw = self.config.env().get("OAOS_PORTAL_ADMINS", "")
        return [item for item in re.split(r"[,\s]+", raw) if MM_ID_RE.match(item)]

    # Step 1: application -> mail code.
    def apply(self, form, ip):
        name = form.get("name", "").strip()
        email = form.get("email", "").strip().lower()
        org = form.get("org", "").strip()
        purpose = form.get("purpose", "").strip()
        if not self.config.apply_open():
            return "closed", None
        if form.get("website"):
            return "ignored", None  # hidden field filled: a bot
        if form.get("consent") != "yes":
            return "consent", None
        if not (1 <= len(name) <= 80 and len(org) <= 120 and 1 <= len(purpose) <= 1000 and EMAIL_RE.match(email)):
            return "invalid", None
        ip_key = "ip:" + hashlib.sha256(ip.encode()).hexdigest()[:24]
        mail_key = "mail:" + hashlib.sha256(email.encode()).hexdigest()[:24]
        if not self.store.rate_hit(ip_key, self.config.ip_limit) or not self.store.rate_hit(mail_key, self.config.email_limit):
            return "rate", None
        open_request = self.store.one("SELECT id FROM requests WHERE email = ? AND state IN ('pending','approved')", (email,))
        if open_request:
            return "exists", None
        request_id = secrets.token_urlsafe(12)
        code = "%06d" % secrets.randbelow(1000000)
        with self.store.tx() as db:
            db.execute("INSERT INTO requests(id, created_at, name, email, org, purpose, state, code_hash, code_expires)"
                       " VALUES (?,?,?,?,?,?, 'unverified', ?, ?)",
                       (request_id, now(), name, email, org, purpose, code_hash(self.secret(), request_id, code),
                        now() + CODE_TTL))
            self.store.audit(db, "applicant", "apply", request_id, "code-sent")
        try:
            self.out.send_mail(email, "[OAOS] 이용 신청 확인 코드 / verification code",
                               "OAOS Project 이용 신청 확인 코드: %s\n(10분 안에 신청 화면에 입력하세요.)\n\n"
                               "Verification code: %s (valid for 10 minutes).\n\n"
                               "신청하지 않았다면 이 메일을 무시하세요. / Ignore this mail if you did not apply.\n" % (code, code))
        except Exception as error:  # noqa: BLE001 - any SMTP failure ends this attempt
            with self.store.tx() as db:
                db.execute("DELETE FROM requests WHERE id = ? AND state = 'unverified'", (request_id,))
                self.store.audit(db, "portal", "apply", request_id, "mail-failed", type(error).__name__)
            return "mail", None
        return "code", request_id

    # Step 2: code -> pending -> admin DM.
    def verify(self, request_id, code):
        row = self.store.one("SELECT * FROM requests WHERE id = ?", (request_id,))
        if not row or row["state"] != "unverified":
            return "unknown"
        if row["code_expires"] < now() or row["code_attempts"] >= CODE_ATTEMPTS:
            with self.store.tx() as db:
                db.execute("UPDATE requests SET state = 'expired', code_hash = NULL WHERE id = ? AND state = 'unverified'", (request_id,))
                self.store.audit(db, "applicant", "verify", request_id, "expired")
            return "expired"
        if not re.match(r"^\d{6}$", code or "") or not hmac.compare_digest(code_hash(self.secret(), request_id, code), row["code_hash"]):
            with self.store.tx() as db:
                db.execute("UPDATE requests SET code_attempts = code_attempts + 1 WHERE id = ?", (request_id,))
                self.store.audit(db, "applicant", "verify", request_id, "wrong-code")
            return "wrong"
        with self.store.tx() as db:
            db.execute("UPDATE requests SET state = 'pending', verified_at = ?, code_hash = NULL WHERE id = ?", (now(), request_id))
            self.store.audit(db, "applicant", "verify", request_id, "pending")
        self.notify_admins(request_id)
        return "pending"

    def decision_attachment(self, row, secret):
        fields = [{"title": "이름 / Name", "value": row["name"], "short": True},
                  {"title": "메일 / Mail", "value": row["email"], "short": True},
                  {"title": "소속 / Org", "value": row["org"] or "-", "short": True},
                  {"title": "목적 / Purpose", "value": row["purpose"], "short": False}]
        actions = []
        for action, label, style in (("approve", "승인 Approve", "primary"), ("reject", "거절 Reject", "danger")):
            actions.append({"id": action, "name": label, "style": style, "integration": {
                "url": self.config.callback_url + "/api/portal/decide",
                "context": {"request_id": row["id"], "action": action, "sig": sign(secret, row["id"], action)}}})
        return [{"fallback": "OAOS 이용 신청", "title": "OAOS Project 이용 신청 / access request",
                 "fields": fields, "actions": actions}]

    def notify_admins(self, request_id):
        row = self.store.one("SELECT * FROM requests WHERE id = ?", (request_id,))
        attachments = self.decision_attachment(row, self.secret())
        sent = sum(self.out.dm(admin, "새 이용 신청이 있습니다. 승인 또는 거절을 눌러 주세요.", attachments) for admin in self.admins())
        with self.store.tx() as db:
            self.store.audit(db, "portal", "notify-admins", request_id, "sent" if sent else "failed", "%d admin(s)" % sent)
        return sent

    def decider_allowed(self, user_id, team_id):
        """Re-reads the decider's role now: system_admin, or admin of the portal team."""
        status, user = self.out.mm("GET", "/users/" + user_id)
        if status != 200 or not isinstance(user, dict) or user.get("delete_at"):
            return False, None
        if "system_admin" in str(user.get("roles", "")).split():
            return True, user
        team = self.config.env().get("OAOS_PORTAL_TEAM_ID", "") or team_id or ""
        if MM_ID_RE.match(team):
            status, member = self.out.mm("GET", "/teams/%s/members/%s" % (team, user_id))
            if status == 200 and isinstance(member, dict) and member.get("delete_at", 0) == 0 and (
                    member.get("scheme_admin") is True or "team_admin" in str(member.get("roles", "")).split()):
                return True, user
        return False, user

    # Step 3: Mattermost button callback.
    def decide(self, payload):
        context = payload.get("context") or {}
        request_id, action, sig = context.get("request_id", ""), context.get("action", ""), context.get("sig", "")
        user_id = str(payload.get("user_id", ""))
        if action not in ("approve", "reject") or not isinstance(sig, str) or not MM_ID_RE.match(user_id):
            return 400, {"ephemeral_text": "잘못된 요청입니다. / Invalid request."}
        if not hmac.compare_digest(sign(self.secret(), str(request_id), action), sig):
            with self.store.tx() as db:
                self.store.audit(db, user_id, "decide", str(request_id)[:32], "bad-signature")
            return 403, {"ephemeral_text": "서명이 맞지 않습니다. / Signature mismatch."}
        row = self.store.one("SELECT * FROM requests WHERE id = ?", (request_id,))
        if not row:
            return 404, {"ephemeral_text": "신청을 찾을 수 없습니다. / Request not found."}
        allowed, user = self.decider_allowed(user_id, payload.get("team_id"))
        if not allowed:
            with self.store.tx() as db:
                self.store.audit(db, user_id, "decide", request_id, "denied-role")
            return 200, {"ephemeral_text": "Project 관리자(시스템 관리자 또는 팀 관리자)만 결정할 수 있습니다. / Only a Project administrator can decide."}
        user_email = str((user or {}).get("email", "")).strip().lower()
        if (user_email and user_email == (row["email"] or "")) or user_id == row["mattermost_user_id"]:
            with self.store.tx() as db:
                self.store.audit(db, user_id, "decide", request_id, "denied-self")
            return 200, {"ephemeral_text": "본인 신청은 다른 관리자가 결정해야 합니다. / You cannot decide your own request."}
        decision = "approved" if action == "approve" else "rejected"
        with self.store.tx() as db:
            current = db.execute("SELECT state FROM requests WHERE id = ?", (request_id,)).fetchone()["state"]
            if current != "pending":
                self.store.audit(db, user_id, "decide", request_id, "already-" + current)
                return 200, {"ephemeral_text": "이미 처리된 신청입니다(%s). / Already %s." % (current, current)}
            db.execute("UPDATE requests SET state = ?, decided_at = ? WHERE id = ?", (decision, now(), request_id))
            db.execute("INSERT INTO decisions(request_id, decider_id, decision, decided_at) VALUES (?,?,?,?)",
                       (request_id, user_id, decision, now()))
            self.store.audit(db, user_id, "decide", request_id, decision)
        notes = self.after_approval(request_id) if decision == "approved" else []
        self.result_mail(request_id, decision)
        label = "승인됨 / Approved" if decision == "approved" else "거절됨 / Rejected"
        text = "%s — %s (%s) · @%s" % (label, row["name"], row["email"], (user or {}).get("username", user_id))
        if notes:
            text += "\n" + "\n".join("- " + note for note in notes)
        return 200, {"update": {"message": text, "props": {"attachments": []}}}

    # Step 4: invites after approval (bot and agent accounts stay non-admin; no account creation).
    def after_approval(self, request_id):
        row = self.store.one("SELECT * FROM requests WHERE id = ?", (request_id,))
        notes, mm_result, outline_result = [], "manual", "manual"
        team = self.config.env().get("OAOS_PORTAL_TEAM_ID", "")
        if MM_ID_RE.match(team):
            status, _ = self.out.mm("POST", "/teams/%s/invite/email" % team, [row["email"]])
            if status in (200, 201):
                mm_result = "email"
            else:
                status, body = self.out.mm("GET", "/teams/" + team)
                invite_id = body.get("invite_id") if status == 200 and isinstance(body, dict) else ""
                if invite_id and re.match(r"^[A-Za-z0-9]{8,64}$", invite_id):
                    mm_result = "link:%s/signup_user_complete/?id=%s" % (self.config.chat_url, invite_id)
        if mm_result == "manual":
            notes.append("Mattermost 팀 초대를 직접 보내 주세요(메일 초대가 꺼져 있거나 권한이 없음). / Invite to the team manually.")
        status, _ = self.out.outline("users.invite", {"invites": [{"email": row["email"], "name": row["name"], "role": "member"}]})
        if status == 200:
            outline_result = "invited"
        else:
            notes.append("Outline 초대를 직접 보내 주세요(구성원 초대가 허용되지 않음). / Invite to Outline manually.")
        with self.store.tx() as db:
            db.execute("UPDATE requests SET invite_mattermost = ?, invite_outline = ? WHERE id = ?",
                       (mm_result, outline_result, request_id))
            self.store.audit(db, "portal", "invite", request_id, "mattermost=%s outline=%s" % (
                mm_result.split(":", 1)[0], outline_result))
        return notes

    def result_mail(self, request_id, decision):
        row = self.store.one("SELECT * FROM requests WHERE id = ?", (request_id,))
        if decision == "approved":
            mm = row["invite_mattermost"] or "manual"
            if mm == "email":
                step = "1. Mattermost 팀 초대 메일의 링크로 가입하세요. / Join through the Mattermost invitation mail."
            elif mm.startswith("link:"):
                step = "1. 다음 링크로 Mattermost에 가입하세요. / Sign up here: %s" % mm[5:]
            else:
                step = "1. 관리자가 Mattermost 팀 초대를 보낼 예정입니다. / An administrator will invite you to Mattermost."
            outline = ("2. Outline 초대 메일로 위키에 합류하세요. / Join Outline through its invitation mail."
                       if row["invite_outline"] == "invited" else
                       "2. 관리자가 Outline 초대를 보낼 예정입니다. / An administrator will invite you to Outline.")
            text = ("%s님, OAOS Project 이용 신청이 승인되었습니다.\nYour access request was approved.\n\n%s\n%s\n"
                    "3. 가입 후 몇 분 안에 에이전트 봇과 대화할 수 있게 됩니다. / The agent bot accepts your messages a few minutes after you sign up.\n\n"
                    "Mattermost: %s\nOutline: %s\n" % (row["name"], step, outline, self.config.chat_url, self.config.note_url))
            subject = "[OAOS] 이용 신청 승인 / request approved"
        else:
            text = ("%s님, OAOS Project 이용 신청이 승인되지 않았습니다. 문의는 포털의 연락처로 해 주세요.\n"
                    "Your access request was not approved. Please use the contact on the portal for questions.\n" % row["name"])
            subject = "[OAOS] 이용 신청 결과 / request result"
        try:
            self.out.send_mail(row["email"], subject, text)
            result = "sent"
        except Exception as error:  # noqa: BLE001
            result = "failed:" + type(error).__name__
        with self.store.tx() as db:
            self.store.audit(db, "portal", "result-mail", request_id, result)

    # Background: expiry, retention, allowlist sync.
    def sync(self):
        cutoff = now() - self.config.retention_days * 86400
        with self.store.tx() as db:
            for row in db.execute("SELECT id FROM requests WHERE state = 'unverified' AND code_expires < ?", (now(),)).fetchall():
                db.execute("UPDATE requests SET state = 'expired', code_hash = NULL WHERE id = ?", (row["id"],))
                self.store.audit(db, "portal", "expire", row["id"], "expired")
            purge = db.execute("SELECT id FROM requests WHERE purged_at IS NULL AND ("
                               "(state IN ('rejected','expired') AND COALESCE(decided_at, verified_at, created_at) < ?)"
                               " OR (state = 'expired' AND verified_at IS NULL AND created_at < ?))",
                               (cutoff, now() - UNVERIFIED_PURGE)).fetchall()
            for row in purge:
                db.execute("UPDATE requests SET name = NULL, email = NULL, org = NULL, purpose = NULL, purged_at = ? WHERE id = ?",
                           (now(), row["id"]))
                self.store.audit(db, "portal", "purge", row["id"], "personal-data-removed")
            db.execute("DELETE FROM rate WHERE at < ?", (now() - 3600,))
        return self.sync_allowlist()

    def sync_allowlist(self):
        waiting = self.store.all("SELECT id, email FROM requests WHERE state = 'approved' AND allowlisted_at IS NULL AND email IS NOT NULL")
        found = []
        for row in waiting:
            status, user = self.out.mm("GET", "/users/email/" + urllib.parse.quote(row["email"], safe=""))
            if status == 200 and isinstance(user, dict) and MM_ID_RE.match(str(user.get("id", ""))) and \
                    str(user.get("email", "")).lower() == row["email"] and not user.get("is_bot") and not user.get("delete_at"):
                found.append((row["id"], user["id"]))
        if not found:
            return 0
        env = self.config.env()
        current = [item for item in re.split(r"[,\s]+", env.get("MATTERMOST_ALLOWED_USERS", "")) if MM_ID_RE.match(item)]
        merged = current + [user_id for _, user_id in found if user_id not in current]
        if merged != current:
            env_put(self.config.env_file, "MATTERMOST_ALLOWED_USERS", ",".join(merged))
        restarted = self.out.restart_gateway() if merged != current else True
        with self.store.tx() as db:
            for request_id, user_id in found:
                db.execute("UPDATE requests SET mattermost_user_id = ?, allowlisted_at = ? WHERE id = ?", (user_id, now(), request_id))
                self.store.audit(db, "portal", "allowlist", request_id, "added", "user %s" % user_id)
            self.store.audit(db, "portal", "gateway-restart", None, "ok" if restarted else "failed", "%d user(s)" % len(found))
        if not restarted:
            for admin in self.admins():
                self.out.dm(admin, "MATTERMOST_ALLOWED_USERS를 갱신했지만 게이트웨이 재시작에 실패했습니다. `hermes gateway restart`를 실행해 주세요.")
        return len(found)


# --- HTTP ---------------------------------------------------------------------------------

STYLE = ("body{font-family:system-ui,-apple-system,'Noto Sans KR',sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;"
         "line-height:1.6;color:#1f2933}label{display:block;margin-top:1rem;font-weight:600}input,textarea{width:100%;"
         "padding:.5rem;font:inherit;box-sizing:border-box}button{margin-top:1.2rem;padding:.6rem 1.4rem;font:inherit}"
         ".hp{position:absolute;left:-9999px}.note{color:#52606d;font-size:.95rem}.consent label{font-weight:400}"
         ".consent input{width:auto}")

MESSAGES = {
    "closed": ("지금은 이용 신청을 받지 않습니다.", "Applications are closed at the moment."),
    "consent": ("개인정보 수집·이용에 동의해야 신청할 수 있습니다.", "Please accept the personal data notice."),
    "invalid": ("입력값을 확인해 주세요(이름·메일·사용 목적은 필수).", "Please check the form (name, mail and purpose are required)."),
    "rate": ("신청이 너무 많습니다. 한 시간 뒤 다시 시도해 주세요.", "Too many attempts; try again in an hour."),
    "exists": ("이 메일로 접수된 신청이 이미 처리 중이거나 승인되었습니다.", "A request for this mail is already in progress or approved."),
    "mail": ("확인 메일을 보내지 못했습니다. 잠시 뒤 다시 시도해 주세요.", "The verification mail could not be sent; try again later."),
    "csrf": ("페이지가 만료되었습니다. 신청 화면을 새로 열어 주세요.", "The page expired; open the form again."),
    "unknown": ("신청을 찾을 수 없습니다. 처음부터 다시 신청해 주세요.", "Request not found; please apply again."),
    "expired": ("확인 코드가 만료되었거나 시도 횟수를 넘었습니다. 다시 신청해 주세요.", "The code expired or too many attempts; please apply again."),
    "wrong": ("확인 코드가 맞지 않습니다.", "The code does not match."),
    "pending": ("신청이 접수되었습니다. 관리자가 승인하면 메일로 알려 드립니다.", "Your request was received. You will get a mail once an administrator decides."),
    "ignored": ("신청이 접수되었습니다.", "Received."),
}


def page(title, body):
    return ("<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s</title><style>%s</style></head><body>%s<p class=\"note\"><a href=\"/\">← 포털 / Portal</a></p>"
            "</body></html>" % (html.escape(title), STYLE, body)).encode("utf-8")


def message_page(kind):
    korean, english = MESSAGES[kind]
    return page("OAOS Project", "<h1>OAOS Project</h1><p>%s</p><p class=\"note\">%s</p>" % (html.escape(korean), html.escape(english)))


def apply_form(csrf, error=None):
    alert = ""
    if error:
        alert = "<p><strong>%s</strong><br><span class=\"note\">%s</span></p>" % tuple(html.escape(x) for x in MESSAGES[error])
    return page("이용 신청 — OAOS Project", """<h1>이용 신청 <small class="note">Access request</small></h1>%s
<form method="post" action="/api/portal/apply">
<input type="hidden" name="csrf" value="%s">
<label>이름 / Name<input name="name" maxlength="80" required></label>
<label>메일 / Mail<input name="email" type="email" maxlength="254" required></label>
<label>소속 / Organisation<input name="org" maxlength="120"></label>
<label>사용 목적 / Purpose<textarea name="purpose" rows="4" maxlength="1000" required></textarea></label>
<label class="hp">Website<input name="website" tabindex="-1" autocomplete="off"></label>
<div class="consent"><label><input type="checkbox" name="consent" value="yes" required>
개인정보 수집·이용 동의: 이름·메일·소속·사용 목적을 이용 신청 처리(확인 메일, 관리자 승인, 초대)에만 씁니다.
거절·만료된 신청의 개인정보는 보존 기간이 지나면 지웁니다.
<span class="note">I agree that my name, mail, organisation and purpose are used only to process this request; data of rejected or expired requests is deleted after the retention period.</span></label></div>
<button type="submit">확인 코드 받기 / Send code</button>
</form>""" % (alert, html.escape(csrf)))


def code_form(csrf, request_id, error=None):
    alert = ""
    if error:
        alert = "<p><strong>%s</strong><br><span class=\"note\">%s</span></p>" % tuple(html.escape(x) for x in MESSAGES[error])
    return page("확인 코드 — OAOS Project", """<h1>확인 코드 <small class="note">Verification code</small></h1>%s
<p>입력한 메일로 6자리 코드를 보냈습니다(10분 유효).<br><span class="note">We sent a 6-digit code to your mail (valid for 10 minutes).</span></p>
<form method="post" action="/api/portal/verify">
<input type="hidden" name="csrf" value="%s"><input type="hidden" name="request" value="%s">
<label>코드 / Code<input name="code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required></label>
<button type="submit">신청하기 / Submit</button>
</form>""" % (alert, html.escape(csrf), html.escape(request_id)))


class Handler(http.server.BaseHTTPRequestHandler):
    portal = None
    server_version = "oaos-portal"
    sys_version = ""

    def log_message(self, fmt, *args):  # request lines only; never bodies or headers
        sys.stderr.write("oaos-portal %s %s\n" % (self.command, self.path.split("?", 1)[0]))

    def client_ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def send(self, status, body, content_type="text/html; charset=utf-8", cookie=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", "oaos_portal_csrf=%s; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=3600" % cookie)
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, data):
        self.send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json")

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(length)

    def csrf_cookie(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == "oaos_portal_csrf":
                return value
        return ""

    def form(self):
        fields = urllib.parse.parse_qs(self.body().decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=20)
        return {key: values[0] for key, values in fields.items()}

    def csrf_ok(self, form):
        cookie, field = self.csrf_cookie(), form.get("csrf", "")
        return bool(cookie) and len(cookie) >= 20 and hmac.compare_digest(cookie, field)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/portal/health":
            self.json(200, {"status": "ok", "schema": SCHEMA_VERSION, "apply": "on" if self.portal.config.apply_open() else "off"})
        elif path in ("/apply", "/apply/"):
            if not self.portal.config.apply_open():
                self.send(200, message_page("closed"))
                return
            token = secrets.token_urlsafe(24)
            self.send(200, apply_form(token), cookie=token)
        else:
            self.send(404, message_page("unknown"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/portal/decide":
                payload = json.loads(self.body().decode("utf-8") or "{}")
                status, data = self.portal.decide(payload if isinstance(payload, dict) else {})
                self.json(status, data)
                return
            form = self.form()
            if path not in ("/api/portal/apply", "/api/portal/verify"):
                self.send(404, message_page("unknown"))
                return
            if not self.csrf_ok(form):
                self.send(403, message_page("csrf"))
                return
            csrf = form["csrf"]
            if path == "/api/portal/apply":
                result, request_id = self.portal.apply(form, self.client_ip())
                if result == "code":
                    self.send(200, code_form(csrf, request_id))
                elif result in ("closed", "ignored", "exists", "rate", "mail"):
                    self.send(429 if result == "rate" else 200, message_page(result))
                else:
                    self.send(400, apply_form(csrf, result))
                return
            result = self.portal.verify(form.get("request", ""), form.get("code", "").strip())
            if result == "wrong":
                self.send(400, code_form(csrf, form.get("request", ""), "wrong"))
            else:
                self.send(200, message_page(result))
        except (ValueError, UnicodeDecodeError):
            self.send(400, message_page("invalid"))
        except RuntimeError as error:
            sys.stderr.write("oaos-portal configuration error: %s\n" % error)
            self.send(503, message_page("mail"))


def sync_loop(portal, stop):
    while not stop.wait(portal.config.sync_seconds):
        try:
            portal.sync()
        except Exception as error:  # noqa: BLE001 - keep the loop alive; report the class only
            sys.stderr.write("oaos-portal sync error: %s\n" % type(error).__name__)


def main(argv):
    config = Config(os.environ)
    store = Store(config.db_path)
    portal = Portal(config, store, Outbound(config))
    if len(argv) > 1 and argv[1] == "--sync-once":
        print(portal.sync())
        return 0
    if len(argv) > 1 and argv[1] == "--init":
        return 0
    Handler.portal = portal
    server = http.server.ThreadingHTTPServer(("127.0.0.1", config.port), Handler)
    server.daemon_threads = True
    stop = threading.Event()
    threading.Thread(target=sync_loop, args=(portal, stop), daemon=True).start()
    sys.stderr.write("oaos-portal listening on 127.0.0.1:%d\n" % config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
