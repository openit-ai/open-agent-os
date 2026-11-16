# outline-wiki — skill guide

> Lets Hermes Agent search, read, create and update documents in the team's Outline wiki through the Outline API — as the non-admin AI-agent account of the OAOS Project edition, with a zero-dependency helper (Python stdlib only).

- **Version**: 0.1.0 · **Platform**: Linux, macOS (Hermes Agent; the OAOS Project host is Ubuntu)
- **Location (installed)**: `~/.hermes/skills/outline-wiki/` (or `$HERMES_HOME/skills/outline-wiki/`)
- **Installed by**: `editions/project/install.sh` (`team` stage, once gate G8 passes)

## 1. What it does

| Command | Outline endpoint | Purpose |
|---|---|---|
| `whoami` | `auth.info` | The account behind the token (name, e-mail, role) |
| `collections` | `collections.list` | Collections the agent can read (ids for `create`) |
| `search` | `documents.search` | Full-text search; ids, titles, urls, snippet |
| `info` | `documents.info` | One document including its markdown text |
| `create` | `documents.create` | New document in a collection (published unless `--draft`) |
| `update` | `documents.update` | New title and/or body; `--append` adds to the end |

Design rules: **member endpoints only** — no generic passthrough, nothing from the admin API · **token never on a command line** — read from the Hermes `.env`, sent in the HTTPS `Authorization` header, redirects are not followed · **JSON in, JSON out** — one object per call (`ok`, `data` or `error`, `status`) · **stdlib only**.

## 2. Structure

```
outline-wiki/
├── SKILL.md            # the runbook Hermes loads
├── README.md           # this document
└── scripts/
    └── outline.py      # the helper — Python stdlib only
```

## 3. Prerequisites

- OAOS Project gate **G8**: a human admin invited the AI-agent account (its e-mail is the G9 bot mailbox) as a member or editor; that account created an API token; the owner stored it with `bash bootstrap/bin/oaos-secret-set OUTLINE_API_TOKEN`. The `team` stage checks it with `auth.info` (agent e-mail, not admin) and stores `OUTLINE_URL`.
- Values: `OUTLINE_URL` (https) and `OUTLINE_API_TOKEN` — read from the process environment when present, otherwise from `$HERMES_HOME/.env` (default `~/.hermes/.env`). `OUTLINE_API_BASE` may point at a loopback listener (for example `http://127.0.0.1:3000` on the Project host); plain http is refused for any other host.

## 4. Usage

```bash
OW=${HERMES_HOME:-$HOME/.hermes}/skills/outline-wiki/scripts/outline.py
python3 "$OW" whoami
python3 "$OW" search --query "onboarding" --limit 5
python3 "$OW" info --id <document-id>
python3 "$OW" collections
python3 "$OW" create --title "Weekly notes" --collection <collection-id> --text-file notes.md
python3 "$OW" update --id <document-id> --text-file more.md --append
```

Exit codes: `0` success · `1` the API refused the request or could not be reached (`status` holds the HTTP code) · `2` configuration or usage error.

## 5. Verification

- `bash bootstrap/tests/outline-skill-smoke.sh` runs every command against a local mock of the Outline API and checks that the token never appears in output or argv, that plain http to a non-loopback host is refused, and that no admin endpoint is reached.
- On a Project host, `bash bootstrap/verify/project-verify.sh` runs `whoami` and a read-only `search` through the installed helper (row "Outline skill").

## 6. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `"status": 401` | token missing, revoked or mistyped | issue a new token from the agent account (Settings → API Keys), store it with the secret helper, rerun the Project `team` stage |
| `"status": 403` | the agent account cannot access that collection or document | a human admin shares the collection with the agent account |
| exit 2, `OUTLINE_URL must be an https URL` | `OUTLINE_URL` absent or not https | rerun the Project `team` stage (it stores `https://<note domain>`) |
| `network_error` | Outline down or DNS not reachable from this host | `bash bootstrap/verify/project-verify.sh` (Outline health row) |

## 7. References

- Outline API: https://www.getoutline.com/developers
- OAOS Project edition: `editions/project/README.md` (gates G8 and G9)

---
Part of the [open-agent-os](https://github.com/openit-ai/open-agent-os) repository · License: Apache-2.0
