#!/usr/bin/env python3
"""C09 management console: four read-only tabs behind an authenticated session.

The process binds loopback only; nginx terminates TLS for /company/. Session
tokens are random cookies whose SHA-256 is the only value stored in PostgreSQL.
Every tab is produced by company.console_snapshot(), which applies the
administrator/member scope and returns metadata only (never Vault ciphertext).
"""

import hashlib
import hmac
import html
import json
import os
import subprocess
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("OAOS_COMPANY_CONSOLE_PORT", "8765"))
SESSION_COOKIE = "oaos_company_session"
CSRF_COOKIE = "oaos_company_csrf"
TABS = ("members", "integrations", "policies", "status")
TAB_LABELS = {
    "members": "구성원",
    "integrations": "연동",
    "policies": "정책",
    "status": "상태",
}
MAX_FORM_BYTES = 4096


def _psql(statements, variables=None, timeout=10):
    # psql expands :'name' variables when it reads a script from stdin, but not
    # inside -c strings. Feeding the script through stdin keeps every value a
    # quoted psql variable instead of string-concatenating it into SQL.
    command = ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-f", "-"]
    for key, value in (variables or {}).items():
        command.extend(["-v", f"{key}={value}"])
    script = "\n".join(statements) + "\n"
    return subprocess.run(
        command, input=script, check=True, capture_output=True, text=True,
        timeout=timeout
    ).stdout


def _last_line(output):
    for line in reversed(output.splitlines()):
        if line:
            return line
    return ""


def authenticate(token):
    if not token or len(token) > 256:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        output = _psql(
            [
                "SELECT 'AUTH|' || session_id::text || '|' || org_id::text || '|' "
                "|| member_id::text || '|' || role || '|' || csrf_hash || '|' "
                "|| extract(epoch from expires_at)::bigint "
                "FROM company.console_authenticate(:'hash');",
            ],
            {"hash": token_hash},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = _last_line(output)
    if not line.startswith("AUTH|"):
        return None
    parts = line.split("|")
    if len(parts) != 7:
        return None
    _, session_id, org_id, member_id, role, csrf_hash, expires = parts
    if role not in ("admin", "member"):
        return None
    if not all(
        _is_uuid(value) for value in (session_id, org_id, member_id)
    ):
        return None
    if not _is_hash(csrf_hash):
        return None
    return {
        "session_id": session_id,
        "org_id": org_id,
        "member_id": member_id,
        "role": role,
        "csrf_hash": csrf_hash,
        "expires_at": expires,
    }


def snapshot(session, tab):
    if tab not in TABS:
        return {"decision": "invalid", "tab": tab, "data": {}}
    try:
        output = _psql(
            [
                "SELECT set_config('company.org_id', :'org', false);",
                "SELECT set_config('company.member_id', :'member', false);",
                "SELECT company.console_snapshot(:'tab')::text;",
            ],
            {
                "org": session["org_id"],
                "member": session["member_id"],
                "tab": tab,
            },
        )
    except (OSError, subprocess.SubprocessError):
        return {"decision": "error", "tab": tab, "data": {}}
    line = _last_line(output)
    try:
        value = json.loads(line)
    except (TypeError, ValueError):
        return {"decision": "error", "tab": tab, "data": {}}
    return value if isinstance(value, dict) else {"decision": "error", "data": {}}


def revoke_session(session):
    try:
        output = _psql(
            [
                "SELECT set_config('company.org_id', :'org', false);",
                "SELECT set_config('company.member_id', :'member', false);",
                "SELECT company.console_revoke_session(:'session');",
            ],
            {
                "org": session["org_id"],
                "member": session["member_id"],
                "session": session["session_id"],
            },
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return _last_line(output) == "revoked"


def service_state():
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "oaos-company.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    state = result.stdout.strip()
    return state if state in ("active", "inactive", "failed") else "unknown"


def _is_uuid(value):
    if not value or len(value) != 36:
        return False
    groups = value.split("-")
    if [len(part) for part in groups] != [8, 4, 4, 4, 12]:
        return False
    return all(
        character in "0123456789abcdefABCDEF"
        for part in groups
        for character in part
    )


def _is_hash(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _escape(value):
    return html.escape("" if value is None else str(value), quote=True)


def _render_value(value):
    if isinstance(value, (dict, list)):
        return "<code>" + _escape(json.dumps(value, ensure_ascii=False, sort_keys=True)) + "</code>"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _escape(value)


def _render_rows(rows):
    if not rows:
        return "<p class=\"empty\">표시할 항목이 없습니다.</p>"
    columns = []
    for row in rows:
        if isinstance(row, dict):
            for key in row:
                if key not in columns:
                    columns.append(key)
    if not columns:
        return "<p class=\"empty\">표시할 항목이 없습니다.</p>"
    head = "".join(f"<th>{_escape(column)}</th>" for column in columns)
    body = []
    for row in rows:
        cells = "".join(
            f"<td>{_render_value(row.get(column))}</td>" for column in columns
        )
        body.append(f"<tr>{cells}</tr>")
    return (
        "<table><thead><tr>"
        + head
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


def _render_data(data):
    if not isinstance(data, dict) or not data:
        return "<p class=\"empty\">표시할 항목이 없습니다.</p>"
    blocks = []
    for key, value in data.items():
        blocks.append(f"<h2>{_escape(key)}</h2>")
        if isinstance(value, list):
            blocks.append(_render_rows(value))
        elif isinstance(value, dict):
            blocks.append(_render_rows([value]))
        else:
            blocks.append(f"<p>{_render_value(value)}</p>")
    return "".join(blocks)


def render_page(session, tab, snapshot_value, csrf=""):
    role = session["role"]
    scope = snapshot_value.get("scope", "self")
    data = snapshot_value.get("data", {})
    if tab == "status" and isinstance(data, dict):
        data = dict(data)
        data["service_state"] = service_state()
    nav = "".join(
        f"<a class=\"{'active' if name == tab else ''}\" "
        f"href=\"/company/console/{name}\">{_escape(TAB_LABELS[name])}</a>"
        for name in TABS
    )
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OAOS Company 관리 콘솔</title>
<style>
body{{font-family:system-ui,-apple-system,"Noto Sans KR",sans-serif;margin:0;background:#f5f6f8;color:#1b1f24}}
header{{background:#14213d;color:#fff;padding:16px 24px;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}}
header h1{{font-size:18px;margin:0}}
.badge{{background:#2b3a67;padding:4px 10px;border-radius:999px;font-size:13px}}
nav{{display:flex;gap:8px;padding:16px 24px;background:#fff;border-bottom:1px solid #dde1e7;flex-wrap:wrap}}
nav a{{text-decoration:none;color:#334155;padding:8px 14px;border-radius:8px;font-weight:600}}
nav a.active{{background:#14213d;color:#fff}}
main{{padding:20px 24px;max-width:1200px}}
table{{border-collapse:collapse;width:100%;background:#fff;margin-bottom:20px;border-radius:8px;overflow:hidden}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #e5e8ec;font-size:14px;vertical-align:top}}
th{{background:#eef1f5;font-weight:700}}
code{{word-break:break-all;font-size:12px}}
.empty{{color:#64748b}}
.meta{{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}}
form{{margin:0}}
button{{background:#14213d;color:#fff;border:0;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer}}
</style></head>
<body>
<header><h1>OAOS Company 관리 콘솔</h1>
<div class="meta"><span class="badge">역할: {_escape(role)}</span>
<span class="badge">범위: {_escape(scope)}</span>
<form method="post" action="/company/console/logout">
<input type="hidden" name="csrf" value="{_escape(csrf)}">
<button type="submit">로그아웃</button></form></div></header>
<nav>{nav}</nav>
<main><h1>{_escape(TAB_LABELS.get(tab, tab))}</h1>
{_render_data(data)}
</main></body></html>
"""


def render_login():
    return """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OAOS Company 로그인</title>
<style>
body{font-family:system-ui,-apple-system,"Noto Sans KR",sans-serif;background:#f5f6f8;color:#1b1f24;display:grid;place-items:center;min-height:100vh;margin:0}
main{background:#fff;padding:32px;border-radius:12px;max-width:560px;box-shadow:0 8px 30px rgba(15,23,42,.08)}
h1{margin-top:0;font-size:22px}
code{background:#eef1f5;padding:2px 6px;border-radius:6px}
</style></head>
<body><main>
<h1>OAOS Company 관리 콘솔</h1>
<p>등록된 Project 계정의 일회 코드 로그인이 필요합니다.</p>
<p>초기 관리자 생성과 로그인 연결은 G10/C10 단계에서 완료됩니다.
C09는 관리자·구성원 화면, 탭별 권한, 비밀 마스킹과 서비스 상태를 제공합니다.</p>
<p><code>/company/console/members</code>, <code>/company/console/integrations</code>,
<code>/company/console/policies</code>, <code>/company/console/status</code></p>
</main></body></html>
"""


def render_error(status, message):
    body = (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        "<title>OAOS Company</title></head><body><main><h1>"
        + _escape(message)
        + "</h1></main></body></html>"
    )
    return status, body


class Console(BaseHTTPRequestHandler):
    server_version = "OAOSCompanyConsole/1.0"

    def log_message(self, _format, *_args):
        pass

    def _cookies(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            return {}, {}
        session = cookies[SESSION_COOKIE].value if SESSION_COOKIE in cookies else ""
        csrf = cookies[CSRF_COOKIE].value if CSRF_COOKIE in cookies else ""
        return session, csrf

    def _session(self):
        token, csrf = self._cookies()
        session = authenticate(token)
        if session is None:
            return None, csrf
        return session, csrf

    def _send(self, status, body, content_type="text/html; charset=utf-8",
              extra_headers=None):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
        )
        for key, value in (extra_headers or []):
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location, extra_headers=None):
        headers = [("Location", location)] + list(extra_headers or [])
        self._send(303, "", extra_headers=headers)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/company/health":
            self._send(200, "ok\n", "text/plain; charset=utf-8")
            return
        if path in ("/company", "/company/"):
            self._redirect("/company/console")
            return
        if path == "/company/console":
            self._redirect("/company/console/members")
            return
        if path == "/company/console/login":
            self._send(200, render_login())
            return
        if path.startswith("/company/console/api/"):
            tab = path.rsplit("/", 1)[-1]
            if tab not in TABS:
                self._send(404, json.dumps({"decision": "invalid"}), "application/json")
                return
            session, _csrf = self._session()
            if session is None:
                self._send(401, json.dumps({"decision": "unauthenticated"}), "application/json")
                return
            value = snapshot(session, tab)
            self._send(200, json.dumps(value, ensure_ascii=False), "application/json")
            return
        if path.startswith("/company/console/"):
            tab = path.rsplit("/", 1)[-1]
            if tab not in TABS:
                status, body = render_error(404, "Not found")
                self._send(status, body)
                return
            session, csrf_cookie = self._session()
            if session is None:
                self._redirect("/company/console/login")
                return
            value = snapshot(session, tab)
            if value.get("decision") != "allow":
                status, body = render_error(403, "접근할 수 없습니다.")
                self._send(status, body)
                return
            self._send(200, render_page(session, tab, value, csrf_cookie))
            return
        status, body = render_error(404, "Not found")
        self._send(status, body)

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/company/console/logout":
            session, csrf_cookie = self._session()
            if session is None:
                self._redirect("/company/console/login")
                return
            length = self.headers.get("Content-Length")
            try:
                length = int(length) if length is not None else 0
            except ValueError:
                length = 0
            if length < 0 or length > MAX_FORM_BYTES:
                status, body = render_error(413, "요청이 너무 큽니다.")
                self._send(status, body)
                return
            body = self.rfile.read(length).decode("utf-8", "replace")
            form = urllib.parse.parse_qs(body)
            submitted = form.get("csrf", [""])[0]
            expected = hashlib.sha256(submitted.encode("utf-8")).hexdigest() if submitted else ""
            cookie_csrf = hashlib.sha256(csrf_cookie.encode("utf-8")).hexdigest() if csrf_cookie else ""
            if not (
                hmac.compare_digest(expected, session["csrf_hash"])
                and hmac.compare_digest(cookie_csrf, session["csrf_hash"])
            ):
                status, body = render_error(403, "CSRF 검증에 실패했습니다.")
                self._send(status, body)
                return
            revoke_session(session)
            self._redirect(
                "/company/console/login",
                [
                    (SESSION_COOKIE, "deleted; Path=/company; Secure; HttpOnly; "
                     "SameSite=Strict; Max-Age=0"),
                    (CSRF_COOKIE, "deleted; Path=/company; Secure; HttpOnly; "
                     "SameSite=Strict; Max-Age=0"),
                ],
            )
            return
        if parsed.path == "/company/console/login":
            status, body = render_error(501, "로그인 연결은 G10/C10 단계에서 활성화됩니다.")
            self._send(status, body)
            return
        status, body = render_error(404, "Not found")
        self._send(status, body)

    def do_PUT(self):
        self._send(405, "Method not allowed\n", "text/plain; charset=utf-8")

    do_DELETE = do_PUT
    do_PATCH = do_PUT


def main():
    server = HTTPServer(("127.0.0.1", PORT), Console)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
