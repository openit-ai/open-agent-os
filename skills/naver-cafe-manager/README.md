# naver-cafe-manager — skill guide

> Operate a Naver cafe from a chat request: write posts, review and answer comments, monitor boards for new keyword-matching articles, and collect article text + photos — Playwright-driven, cookie-authenticated, draft-first.
> This document records how the skill was built, what has been verified, and how to run it step by step.

- **Version**: 0.1.0 · **Platform**: Linux (Hermes Agent)
- **Location (installed)**: `~/.hermes/skills/naver-cafe-manager/`
- **Runtime**: Python 3 + Playwright (Chromium, headless) · cookie auth (`NID_AUT` / `NID_SES`)

**How to read this document**
- What it does → §1 · Structure → §2 · Background → §3
- Prerequisites — accounts & setup → §4
- Verified results → §5 · Step-by-step usage → §6 · Troubleshooting → §7

---

## 1. What it does

Pipeline: **cookie auth → board fetch (Playwright, `/f-e/` board view) → filters (keyword · max-age · sold-out) → new-post diff vs state → JSON output → optional price extraction**, plus a read-only collection path for article text and photos.

| # | Function | Script | Role |
|---|---|---|---|
| 1 | Post writing | `scripts/post.py` | fills the editor + screenshot; registers only with `--submit` |
| 2 | Comment review & reply | `scripts/comments.py` | reads the comment list; draft-first reply |
| 3 | Monitoring & alerts | `scripts/watch.py` | keyword + new-post diff; prints new posts only |
| 4 | Collection | `scripts/collect.py` | read-only body text + photos + manifest |

**The 5 operating rules**

1. **Draft-first** — post/comment scripts fill the editor and stop for review; nothing is registered unless `--submit` is passed.
2. **Cookie auth only** — `NID_AUT`/`NID_SES` via `NAVER_COOKIE`; ID/password auto-login is blocked (macro detection, 2FA).
3. **Real browser required** — board and article pages are JavaScript apps; plain HTTP fetches are blocked (`errorCode 9999`).
4. **Read-only collection** — `collect.py` never writes to the cafe.
5. **Pace it** — a few seconds between articles; drafts are reviewed before any submit.

## 2. Structure

```
naver-cafe-manager/
├── SKILL.md                  # the runbook Hermes loads
├── README.md                 # this document
├── config.example.json       # watch config template (cafes · boards · keywords)
└── scripts/
    ├── post.py               # function 1 — draft-first post writer
    ├── comments.py           # function 2 — comment list + reply
    ├── watch.py              # function 3 — board watcher (new-post diff)
    ├── collect.py            # function 4 — article text + photo collector
    ├── setup_watch.py        # interactive config.json creation
    └── register_cookie.py    # cookie onboarding (NID_AUT/NID_SES → NAVER_COOKIE line)
```

## 3. Background

- The pipeline is one skill: `watch.py` carries the board-monitoring core, `post.py` and `comments.py` the write path, `collect.py` the read path.
- Board listings live at `https://cafe.naver.com/f-e/cafes/<club-id>/menus/<board-id>?viewType=L` — a JavaScript SPA. Verified: plain HTTP fetches fail; a real (headless) Playwright browser with a cookie succeeds.
- Articles are fetched mobile-first (`m.cafe.naver.com/ca-fe/web/cafes/<club-id>/articles/<id>`), with a PC fallback through the `cafe_main` iframe (the PC top-level frame holds only the sidebar).
- Monitoring is a diff: each board keeps a state file of seen article ids, so only posts that are new since the last run are emitted; an empty run exits quietly.
- **Draft-first design**: post/comment writes fill the editor and stop for review unless `--submit` is given. Live-submit testing is intentionally left to the operator (target + approval), since submitted posts/comments are public and irreversible.

## 4. Prerequisites — accounts & setup

- **Naver account** with membership in the target cafe and access to its boards.
- **Cookie pair**: `NID_AUT` + `NID_SES` copied from a logged-in desktop browser → stored as one `NAVER_COOKIE` line in a server-side `.env` (never in the repo, never on the command line). No auto-refresh — re-copy when it expires.
- **Python 3 + Playwright/Chromium**: `pip install playwright && playwright install chromium`. Run the scripts with `python3`; `CHROME_PATH` can point at a specific Chromium binary.
- **Config**: `~/.hermes/data/naver-cafe-watch/config.json` (schema: `config.example.json`), created interactively by `scripts/setup_watch.py`.
- **Cost**: none beyond your own server and Naver account.

## 5. Verified results (2026-09-27)

- **Monitoring** — a live board fetch through Playwright + cookie auth returned **92 posts** and correctly flagged **1 new post** inside the lookback window.
- **Collection (single)** — saved title + body + **4 images** with an `images/manifest.csv` manifest.
- **Collection (batch)** — board batch collection ran over **2 articles**.
- **Comment list** — extraction returned **10 visible of 16 declared** comments, with author/text/time fields correct and the "more" pagination state noted in the JSON message.
- **Session** — the full cookie-auth flow ran headless in Chromium (no display).
- **Write path** — post/comment writing is draft-first: the editor is filled and a screenshot is saved; nothing is submitted unless `--submit` is passed. Live submit testing is intentionally left to the operator.

## 6. Step-by-step usage

Install:

```bash
hermes skills install openit-ai/open-agent-os/skills/naver-cafe-manager
```

One-time cookie onboarding:

```bash
python3 scripts/register_cookie.py --env <path>   # interactive paste; omit --env to print the line
```

One-time config:

```bash
python3 scripts/setup_watch.py         # pick cafe / boards / keywords → config.json
```

Run (every `<placeholder>` is yours to fill):

```bash
# 1) write a post (draft first; add --submit to register)
python3 scripts/post.py --cafe <cafe-domain> --club-id <club-id> --menu-id <board-id> \
  --title "Post title" --content-file body.md [--images a.jpg,b.png] [--submit]

# 2) list comments / draft a reply
python3 scripts/comments.py --cafe <cafe-domain> --club-id <club-id> --article <article-id> --list
python3 scripts/comments.py --cafe <cafe-domain> --club-id <club-id> --article <article-id> \
  --message "reply text" [--reply-index N] [--submit]

# 3) monitor a board (new posts only)
python3 scripts/watch.py --cafe <cafe-domain> --club-id <club-id> --board <board-id> \
  [--keywords A,B] [--max-age-days 31] [--with-price]

# 4) collect an article, or the latest N articles of a board
python3 scripts/collect.py --cafe <cafe-domain> --club-id <club-id> --article <article-id>
python3 scripts/collect.py --cafe <cafe-domain> --club-id <club-id> --board <board-id> --limit 3
```

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `auth-or-block: login wall` / `nidlogin` redirect | cookie expired or invalid | re-copy NID_AUT/NID_SES → `register_cookie.py` |
| Empty board, `errorCode 9999` | unauthenticated access | run through the Playwright scripts; check the cookie |
| Board shows no posts | no cafe/board permission | join the cafe / request board access |
| Comment count lower than declared | "more" list not expanded | the JSON reports visible vs declared; open the UI for the rest |
| Latest comment not visible | comment API returns the oldest 100 | check the newest comments in the UI |
| Empty article body | mobile + PC fallback both failed | report as an access failure; don't estimate content or price |
| `NAVER_COOKIE missing` | env not loaded for the run | keep it in the server `.env` and load it before running |
| Chromium fails to launch | binary path differs per install | `playwright install chromium`, or set `CHROME_PATH` |
| Nothing flagged as new | ids already in the state file | expected — the diff only reports new posts (`--no-state` to bypass) |

## 8. References

- Naver cafe: https://cafe.naver.com
- Playwright (Python): https://playwright.dev/python/
- Hermes skills docs: https://hermes-agent.nousresearch.com/docs
- In-skill: `scripts/*.py`, `config.example.json`

---
Part of the [open-agent-os](https://github.com/openit-ai/open-agent-os) repository · License: Apache-2.0
