---
name: outline-wiki
description: "Search, read, create and update Outline wiki documents."
version: 0.1.0
author: OpenIT (openit-ai), Hermes Agent
license: Apache-2.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [outline, wiki, documents, knowledge, oaos, project]
    related_skills: [oaos-ops]
---

# Outline Wiki

Work with the team's Outline wiki through the Outline API, as the **AI-agent account** the OAOS Project installer verified at gate G8 (a member or editor, never a workspace admin). Flow: **search → read → write (create or update) → report the document link**. Documents you create or edit are authored by that agent account.

## When to Use

- "Where is the onboarding doc / what does it say?" — search, then read.
- "Write this up in the wiki / add these notes to the meeting page" — create or update a document.
- "Which collections do we have?" — list collections before creating a document.
- Don't use for: workspace administration (users, groups, permissions, settings, exports) — the agent account has no admin role and this skill calls no admin endpoint; ask a human admin instead.

## Prerequisites

- OAOS Project edition with gate G8 complete: `OUTLINE_URL` and `OUTLINE_API_TOKEN` are in the Hermes `.env` (owner-only). The installer copies this skill to `$HERMES_HOME/skills/outline-wiki/` (default `~/.hermes/skills/outline-wiki/`).
- Python 3 (standard library only). Helper: `scripts/outline.py`. It reads both values itself and sends the token only in an HTTPS `Authorization` header — never pass the token as an argument, never print it, never copy it into chat, files or memory.
- If a value is missing, ask the owner to store it in their own terminal: `bash bootstrap/bin/oaos-secret-set OUTLINE_API_TOKEN` (from the OAOS repository), then rerun the Project `team` stage.

## Procedure

Set `OW=${HERMES_HOME:-$HOME/.hermes}/skills/outline-wiki/scripts/outline.py` and run it with `python3 "$OW" <command>`. Every command prints one JSON object: `{"ok": true, "data": ...}` or `{"ok": false, "error": ..., "status": ...}` (exit 0 / 1 API refused or unreachable / 2 configuration).

1. Check the account once per session when in doubt: `whoami` → name, e-mail and role (expect `member` or `editor`). Completion: `ok` true.
2. Find before you write: `search --query "<words>" [--limit 10] [--collection <id>]`. Completion: candidate ids, titles and urls.
3. Read: `info --id <id>` returns the markdown `text`. Quote only what the user needs. Completion: the document read.
4. Create: `collections` to pick the collection id, write the markdown body to a temporary file, then `create --title "<title>" --collection <id> [--parent <doc id>] --text-file <file>` (`--draft` keeps it unpublished; `--text-file -` reads stdin). Completion: new id and url.
5. Update: `update --id <id> [--title "<title>"] --text-file <file> [--append]`. Prefer `--append` for logs and meeting notes; replacing a whole body needs the user's go-ahead after you showed what changes. Completion: `ok` true and the url.
6. Report the document title and its link (`OUTLINE_URL` + `url`). Delete temporary files you created.

## Pitfalls

- `status` 401 → token missing, revoked or mistyped: ask the owner to issue a new token from the agent account (Settings → API Keys) and store it with the helper; 403 → the agent account lacks access to that collection or document — ask a human admin to share it, do not look for another account.
- Search is full-text over published documents the agent can access; drafts of other people are not visible.
- Outline stores markdown; keep headings and lists simple. Large rewrites lose inline comments — prefer focused updates.
- Do not put secrets, personal data that does not belong in the team wiki, or credentials into documents.

## Verification

- `python3 "$OW" whoami` returns `ok` true with the agent account e-mail (the G9 bot mailbox) and a non-admin role.
- The OAOS Project verifier runs `whoami` and a read-only `search` through this helper (row "Outline skill").
- After a write, `info --id <id>` shows the new text.
