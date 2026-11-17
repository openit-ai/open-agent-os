#!/usr/bin/env python3
"""Minimal Outline API stand-in for smoke tests (loopback only).

Usage: outline_api_mock.py PORT_FILE
Environment: MOCK_OUTLINE_TOKEN (the only accepted bearer token), MOCK_OUTLINE_LOG (one line
per request: "<endpoint> <auth ok|bad>"). Admin endpoints answer 403 so a caller that reached
them is visible in the log. The port is written to PORT_FILE once the server listens.
"""
import http.server
import json
import os
import sys
import uuid

TOKEN = os.environ["MOCK_OUTLINE_TOKEN"]
LOG = os.environ["MOCK_OUTLINE_LOG"]
COLLECTION = {"id": "col-1", "name": "Team", "description": "Shared docs"}
DOCS = {"doc-1": {"id": "doc-1", "title": "Onboarding", "text": "First week: read the handbook.",
                  "collectionId": "col-1", "url": "/doc/onboarding-doc-1", "updatedAt": "2026-11-17T00:00:00Z"}}
MEMBER_ENDPOINTS = {"auth.info", "collections.list", "documents.search", "documents.info",
                    "documents.create", "documents.update"}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        endpoint = self.path.rsplit("/", 1)[-1]
        auth_ok = self.headers.get("Authorization") == "Bearer " + TOKEN
        with open(LOG, "a") as log:
            log.write("%s %s\n" % (endpoint, "ok" if auth_ok else "bad"))
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if not auth_ok:
            return self.reply(401, {"ok": False, "error": "authentication_required", "message": "Authentication required"})
        if endpoint not in MEMBER_ENDPOINTS:
            return self.reply(403, {"ok": False, "error": "authorization_error", "message": "Admin only"})
        if endpoint == "auth.info":
            return self.reply(200, {"ok": True, "data": {"user": {"id": "u-1", "name": "OAOS Agent", "email": "bot@example.com", "role": "member"},
                                                         "team": {"name": "Example", "url": "https://note.example.com"}}})
        if endpoint == "collections.list":
            return self.reply(200, {"ok": True, "data": [COLLECTION]})
        if endpoint == "documents.search":
            query = str(body.get("query", "")).lower()
            hits = [{"context": doc["text"][:40], "document": doc} for doc in DOCS.values()
                    if query in (doc["title"] + " " + doc["text"]).lower()]
            return self.reply(200, {"ok": True, "data": hits[: int(body.get("limit", 10))]})
        doc = DOCS.get(body.get("id", ""))
        if endpoint == "documents.info":
            return self.reply(200, {"ok": True, "data": doc}) if doc else self.reply(404, {"ok": False, "error": "not_found", "message": "Not found"})
        if endpoint == "documents.create":
            new_id = "doc-" + uuid.uuid4().hex[:8]
            DOCS[new_id] = {"id": new_id, "title": body["title"], "text": body.get("text", ""), "collectionId": body["collectionId"],
                            "url": "/doc/" + new_id, "updatedAt": "2026-11-17T00:00:00Z", "publishedAt": "2026-11-17T00:00:00Z" if body.get("publish") else None}
            return self.reply(200, {"ok": True, "data": DOCS[new_id]})
        if not doc:
            return self.reply(404, {"ok": False, "error": "not_found", "message": "Not found"})
        if "title" in body:
            doc["title"] = body["title"]
        if "text" in body:
            doc["text"] = (doc["text"] + "\n" + body["text"]) if body.get("append") else body["text"]
        return self.reply(200, {"ok": True, "data": doc})


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[1] + ".tmp", "w") as handle:
    handle.write("%d\n" % server.server_address[1])
os.rename(sys.argv[1] + ".tmp", sys.argv[1])
server.serve_forever()
