#!/usr/bin/env python3
"""Mock Mattermost + Outline API and SMTP server for bootstrap/tests/project-portal-smoke.sh.

    portal_mocks.py STATE_DIR

Writes STATE_DIR/http.port and STATE_DIR/smtp.port, then serves until killed.
- HTTP: Mattermost /api/v4/* and Outline /api/users.invite; every request is appended to
  STATE_DIR/http.log as JSON (method, path, body); bearer tokens must match MOCK_MM_TOKEN /
  MOCK_OUTLINE_TOKEN or the request gets 401.
- Behaviour switches (files in STATE_DIR, read per request): mm-invite-off (team mail invite
  answers 403), outline-invite-off (users.invite answers 403), signups.json ({email: user_id}
  for /users/email lookups).
- SMTP: plain, AUTH PLAIN accepted for MOCK_SMTP_USER / MOCK_SMTP_PASSWORD; each message is
  written to STATE_DIR/mail/<n>.eml.
"""
import base64
import http.server
import json
import os
import socketserver
import sys
import threading

STATE = sys.argv[1]
BOT = "b" * 26
TEAM = "t" * 26
USERS = {
    "a" * 26: {"username": "admin", "roles": "system_user system_admin", "email": "admin@example.com"},
    "m" * 26: {"username": "member", "roles": "system_user", "email": "member@example.com"},
    "c" * 26: {"username": "teamadmin", "roles": "system_user", "email": "lead@example.com"},
    "s" * 26: {"username": "selfadmin", "roles": "system_user system_admin", "email": "applicant@example.com"},
}
TEAM_ADMINS = {"c" * 26}


def flag(name):
    return os.path.exists(os.path.join(STATE, name))


class HTTP(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_any(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        with open(os.path.join(STATE, "http.log"), "a", encoding="utf-8") as log:
            log.write(json.dumps({"method": method, "path": self.path, "body": body}) + "\n")
        token = (self.headers.get("Authorization") or "").replace("Bearer ", "", 1)
        path = self.path
        if path.startswith("/api/users.invite"):
            if token != os.environ["MOCK_OUTLINE_TOKEN"]:
                return self.reply(401, {"ok": False})
            if flag("outline-invite-off"):
                return self.reply(403, {"ok": False, "error": "authorization_error"})
            return self.reply(200, {"ok": True, "data": {"sent": body["invites"]}})
        if token != os.environ["MOCK_MM_TOKEN"]:
            return self.reply(401, {"id": "api.context.session_expired.app_error"})
        if path == "/api/v4/users/me":
            return self.reply(200, {"id": BOT, "is_bot": True, "roles": "system_user"})
        if path == "/api/v4/users/me/teams":
            return self.reply(200, [{"id": TEAM, "name": "team"}])
        if path == "/api/v4/channels/direct":
            return self.reply(201, {"id": "d" * 26})
        if path == "/api/v4/posts":
            return self.reply(201, {"id": "p" * 26})
        if path.startswith("/api/v4/users/email/"):
            signups = {}
            if flag("signups.json"):
                with open(os.path.join(STATE, "signups.json"), encoding="utf-8") as source:
                    signups = json.load(source)
            email = path.rsplit("/", 1)[1].replace("%40", "@")
            if email in signups:
                return self.reply(200, {"id": signups[email], "email": email, "is_bot": False, "delete_at": 0})
            return self.reply(404, {"id": "app.user.missing_account.const"})
        if path.startswith("/api/v4/users/"):
            user_id = path.rsplit("/", 1)[1]
            if user_id in USERS:
                return self.reply(200, dict(USERS[user_id], id=user_id, delete_at=0))
            return self.reply(404, {})
        if path.startswith("/api/v4/teams/%s/members/" % TEAM):
            user_id = path.rsplit("/", 1)[1]
            return self.reply(200, {"user_id": user_id, "team_id": TEAM, "delete_at": 0,
                                    "scheme_admin": user_id in TEAM_ADMINS, "roles": "team_user"})
        if path == "/api/v4/teams/%s/invite/email" % TEAM:
            if flag("mm-invite-off"):
                return self.reply(403, {"id": "api.team.invite_members.disabled.app_error"})
            return self.reply(200, {"status": "OK"})
        if path == "/api/v4/teams/" + TEAM:
            return self.reply(200, {"id": TEAM, "invite_id": "invite0123456789abcdef"})
        return self.reply(404, {})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")


class SMTP(socketserver.StreamRequestHandler):
    def handle(self):
        def send(line):
            self.wfile.write((line + "\r\n").encode())
        send("220 mock ESMTP")
        authed = False
        while True:
            line = self.rfile.readline().decode(errors="replace").rstrip("\r\n")
            if not line:
                return
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                send("250-mock")
                send("250 AUTH PLAIN")
            elif verb == "AUTH":
                parts = line.split()
                decoded = base64.b64decode(parts[2]).split(b"\0") if len(parts) > 2 else []
                if len(decoded) == 3 and decoded[1].decode() == os.environ["MOCK_SMTP_USER"] and \
                        decoded[2].decode() == os.environ["MOCK_SMTP_PASSWORD"]:
                    authed = True
                    send("235 ok")
                else:
                    send("535 bad credentials")
            elif verb in ("MAIL", "RCPT"):
                send("250 ok" if authed else "530 auth required")
            elif verb == "DATA":
                send("354 go")
                data = []
                while True:
                    chunk = self.rfile.readline().decode(errors="replace")
                    if chunk in (".\r\n", ".\n", ""):
                        break
                    data.append(chunk)
                directory = os.path.join(STATE, "mail")
                os.makedirs(directory, exist_ok=True)
                name = os.path.join(directory, "%03d.eml" % len(os.listdir(directory)))
                with open(name, "w", encoding="utf-8") as target:
                    target.write("".join(data))
                send("250 queued")
            elif verb == "QUIT":
                send("221 bye")
                return
            else:
                send("250 ok")


class ThreadingTCP(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HTTP)
    smtp = ThreadingTCP(("127.0.0.1", 0), SMTP)
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    for name, server in (("smtp.port", smtp), ("http.port", web)):
        with open(os.path.join(STATE, name + ".tmp"), "w") as target:
            target.write(str(server.server_address[1]))
        os.replace(os.path.join(STATE, name + ".tmp"), os.path.join(STATE, name))
    web.serve_forever()


if __name__ == "__main__":
    main()
