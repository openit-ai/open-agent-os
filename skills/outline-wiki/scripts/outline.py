#!/usr/bin/env python3
"""Outline wiki helper for the outline-wiki skill: whoami / collections / search / info / create / update.

Python stdlib only. Prints one JSON object on stdout: {"ok": true, "data": ...} or
{"ok": false, "error": "...", "status": <HTTP status or null>}.
Exit codes: 0 success; 1 the API refused or could not be reached; 2 usage or configuration error.

Configuration (non-admin AI-agent account, stored by the OAOS Project installer, gate G8):
  OUTLINE_URL        workspace URL, e.g. https://note.example.com
  OUTLINE_API_TOKEN  API token of the agent account (secret)
Both are read from the process environment only: Hermes passes the skill's declared
required_environment_variables through to the terminal sandbox. The token is sent only in the Authorization
header of HTTPS requests to OUTLINE_URL; it is never printed, logged or put on a command line.
OUTLINE_API_BASE (optional, non-secret) points the helper at a local listener such as
http://127.0.0.1:3000 on the Outline host itself. Plain http is accepted for loopback only.
Only member-level endpoints are called; there is no generic passthrough to admin endpoints.
"""
import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ENDPOINTS = {
    "whoami": "auth.info",
    "collections": "collections.list",
    "search": "documents.search",
    "info": "documents.info",
    "create": "documents.create",
    "update": "documents.update",
}
TOKEN_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


class ConfigError(Exception):
    pass


def emit(payload, code):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return code


def settings():
    # Hermes passes the skill's declared required_environment_variables through
    # to the terminal/execute_code environment; this helper never opens secret files.
    url = os.environ.get("OUTLINE_URL", "")
    token = os.environ.get("OUTLINE_API_TOKEN", "")
    base = os.environ.get("OUTLINE_API_BASE") or url
    if not base:
        raise ConfigError("OUTLINE_URL is not set; complete the Project team stage (gate G8)")
    if not token:
        raise ConfigError("OUTLINE_API_TOKEN is not set; store it with: bash bootstrap/bin/oaos-secret-set OUTLINE_API_TOKEN")
    if not set(token) <= TOKEN_CHARS or not 8 <= len(token) <= 256:
        raise ConfigError("OUTLINE_API_TOKEN has unexpected characters; store it again with the secret helper")
    parsed = urllib.parse.urlsplit(base)
    host = parsed.hostname or ""
    if parsed.scheme == "http":
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ConfigError("plain http is accepted for loopback addresses only; use the https OUTLINE_URL")
    elif parsed.scheme != "https" or not host:
        raise ConfigError("OUTLINE_URL must be an https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("OUTLINE_URL must not carry credentials, a query or a fragment")
    return base.rstrip("/") + "/api/", token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the Authorization header to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call(api, token, endpoint, body):
    request = urllib.request.Request(
        api + endpoint,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "oaos-outline-wiki/0.1",
        },
    )
    opener = urllib.request.build_opener(NoRedirect)
    timeout = float(os.environ.get("OUTLINE_TIMEOUT", "20"))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            return response.status, payload
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            payload = {}
        return error.code, payload
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, "reason", error)
        return None, {"ok": False, "error": "network_error", "message": str(reason)[:200]}


def read_text(source):
    if source is None:
        return None
    if source == "-":
        return sys.stdin.read()
    with open(source, encoding="utf-8") as handle:
        return handle.read()


def summarize_user(data):
    user = data.get("user") or {}
    team = data.get("team") or {}
    return {
        "user": {"id": user.get("id"), "name": user.get("name"), "email": user.get("email"),
                 "role": user.get("role") or ("admin" if user.get("isAdmin") else "member")},
        "team": {"name": team.get("name"), "url": team.get("url")},
    }


def summarize_document(doc, with_text=False):
    item = {key: doc.get(key) for key in ("id", "title", "url", "collectionId", "parentDocumentId", "updatedAt", "publishedAt")}
    if with_text:
        item["text"] = doc.get("text")
    return item


def build_parser():
    parser = argparse.ArgumentParser(prog="outline.py", description="Outline wiki helper (non-admin agent account).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("whoami", help="show the account behind the token (auth.info)")
    collections = sub.add_parser("collections", help="list collections the agent can read")
    collections.add_argument("--limit", type=int, default=25)
    search = sub.add_parser("search", help="full-text search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--collection", help="restrict to one collection id")
    info = sub.add_parser("info", help="read one document (markdown text included)")
    info.add_argument("--id", required=True, help="document id or url id")
    create = sub.add_parser("create", help="create a document")
    create.add_argument("--title", required=True)
    create.add_argument("--collection", required=True, help="collection id (see: collections)")
    create.add_argument("--parent", help="parent document id")
    create.add_argument("--text-file", help="markdown body file, or - for stdin")
    create.add_argument("--draft", action="store_true", help="keep it as a draft instead of publishing")
    update = sub.add_parser("update", help="update a document")
    update.add_argument("--id", required=True)
    update.add_argument("--title")
    update.add_argument("--text-file", help="markdown body file, or - for stdin")
    update.add_argument("--append", action="store_true", help="append the text instead of replacing the body")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        api, token = settings()
        body = {}
        if args.command == "collections":
            body = {"limit": max(1, min(args.limit, 100))}
        elif args.command == "search":
            body = {"query": args.query, "limit": max(1, min(args.limit, 100))}
            if args.collection:
                body["collectionId"] = args.collection
        elif args.command == "info":
            body = {"id": args.id}
        elif args.command == "create":
            body = {"title": args.title, "collectionId": args.collection, "publish": not args.draft,
                    "text": read_text(args.text_file) or ""}
            if args.parent:
                body["parentDocumentId"] = args.parent
        elif args.command == "update":
            body = {"id": args.id}
            if args.title:
                body["title"] = args.title
            text = read_text(args.text_file)
            if text is not None:
                body["text"] = text
                body["append"] = bool(args.append)
            if len(body) == 1:
                raise ConfigError("update needs --title and/or --text-file")
    except (ConfigError, OSError) as error:
        return emit({"ok": False, "error": str(error), "status": None}, 2)

    status, payload = call(api, token, ENDPOINTS[args.command], body)
    token = ""
    if status is None or status >= 300 or not isinstance(payload, dict) or payload.get("ok") is False:
        error = payload.get("error") if isinstance(payload, dict) else None
        message = payload.get("message") if isinstance(payload, dict) else None
        return emit({"ok": False, "error": error or "request_failed", "message": message, "status": status}, 1)
    data = payload.get("data")
    if args.command == "whoami":
        result = summarize_user(data or {})
    elif args.command == "collections":
        result = [{"id": c.get("id"), "name": c.get("name"), "description": c.get("description")} for c in data or []]
    elif args.command == "search":
        result = []
        for hit in data or []:
            item = summarize_document(hit.get("document") or {})
            item["context"] = hit.get("context")
            result.append(item)
    else:
        result = summarize_document(data or {}, with_text=args.command == "info")
    return emit({"ok": True, "data": result}, 0)


if __name__ == "__main__":
    sys.exit(main())
