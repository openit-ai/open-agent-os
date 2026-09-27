---
name: naver-cafe-manager
description: "Run a Naver cafe: posts, comments, monitoring, collection."
version: 0.1.0
author: OpenIT (openit-ai), Hermes Agent
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [naver, cafe, playwright, monitoring, collection, korea]
    related_skills: [naver-blog-manager]
---

# Naver Cafe Manager

Run Naver cafe operations from a single skill — ① post writing ② comment review & reply ③ article monitoring & alerts ④ body text & photo collection.
Scope is a specific cafe/board; the global cafe-search API area is not covered.

## Standard management functions

| # | Function | Tool | Default policy |
|---|---|---|---|
| 1 | Post writing | `scripts/post.py` | fills the draft + screenshot; registers only with `--submit` |
| 2 | Comment review & reply | `scripts/comments.py` | reads the list / comment is draft-first (`--submit` registers) |
| 3 | Article monitoring & alerts | `scripts/watch.py` + a scheduler | prints new posts only; exits quietly when there are none |
| 4 | Body & photo collection | `scripts/collect.py` | read-only; md + images + manifest.csv |

## When to Use

- Keyword monitoring & alerts for sale posts (second-hand · giveaway · allocation · Store boards) on a specific cafe/board.
- Cafe post writing automation; comment review & reply; article body/photo collection.
- Don't use for: Naver blog work (→ `naver-blog-manager`); cafe sign-up or permission automation.

## Prerequisites

- Auth: cookie only. Keep `NID_AUT` · `NID_SES` as one `NAVER_COOKIE` line in a server `.env`. ID/password auto-login is not possible (macro detection, 2FA).
- Runtime: Python 3 with Playwright + Chromium — `pip install playwright && playwright install chromium`; run the scripts with `python3`. The browser binary path can be overridden with `CHROME_PATH`.
- Config source of truth: `~/.hermes/data/naver-cafe-watch/config.json` (cafes · boards · keywords); `scripts/setup_watch.py` is the interactive way to create it. Schema: `config.example.json`.
- State & output: state `~/.hermes/data/naver-cafe-watch/`, drafts & screenshots `~/.hermes/data/naver-cafe/manage/`, collected material `~/data/naver-cafe/collect/`.
- Cookie registration/verification: see "Cookie registration" at the end.
- Full guide (structure, verified results, step-by-step usage): [README.md](./README.md).

## Function 1 — Post writing (post.py)

```
python3 scripts/post.py \
  --cafe <cafe-domain> --club-id <club-id> --menu-id <board-id> \
  --title "Post title" --content-file body.md [--images a.jpg,b.png] [--submit]
```

1. Default is **draft mode**: enter the editor from the board → fill title/body/images → save a `draft_*.png` screenshot → stop (nothing is registered).
2. With `--submit` it also presses the register button. Irreversible — review the screenshot and use it at low frequency only (macro-detection risk).
3. If board-button entry fails, pass the editor URL directly with `--editor-url`.
4. Output: one JSON line `{status: draft|submitted-unverified|error, screenshots[]}`.
5. **Status: structure implemented — the live register test is left to the operator** (a target cafe/board must be designated first).

## Function 2 — Comment review & reply (comments.py)

```
# list (read-only)
python3 scripts/comments.py --cafe <cafe-domain> --club-id <club-id> --article <article-id> --list
# write (draft-first; --submit registers)
python3 scripts/comments.py --cafe <cafe-domain> --club-id <club-id> --article <article-id> \
    --message "text" [--reply-index N] [--submit]
```

1. List: extracts comment items (author · text · time) as JSON from the mobile article page. Structure (measured 2026-09-27): `ul.comment_list div.comment_item` — author `.nick_name .ellip`, text `.comment_content .txt`, time `.comment_footer .date`. Distinguishes "no comments" from "module not detected" and reports visible-vs-declared counts when "more" is not loaded.
2. Writing: types the text into the comment field → screenshot (`comment_draft_*.png`). `--submit` clicks the register button.
3. `--reply-index N` first tries that comment's reply button (`button.btn_write`); if it fails, it uses the top-level input.
4. **Status: list = live-verified (author/text/time extracted correctly); write/register = structure implemented, live test pending (operator approval).**

## Function 3 — Article monitoring & alerts (watch.py)

```
python3 scripts/watch.py --cafe <cafe-domain> --club-id <club-id> --board <board-id> \
    [--keywords A,B] [--max-age-days 31] [--with-price] [--no-state|--state-dir DIR] [--all] [--report-file FILE]
```

1. Opens the board list in a real Playwright browser (the `/f-e/` view is an SPA — plain requests are blocked; verified 2026-09-18). Cookie auth, desktop UA.
2. Keyword filter → posts showing sold-out markers (판매완료 · 분양완료 · 거래완료 · 마감됨 · 품절 · "sold out") are always excluded.
3. **Posts older than the recent N days (default 31) are excluded** — list rows are parsed for `YYYY.MM.DD.`, and same-day posts that show only `HH:MM` count as today. Rationale: old posts stay in listings, so without the date check years-old posts look like current stock.
4. New-post diff: compared against a state file (`~/.hermes/data/naver-cafe-watch/<club-id>_<board-id>.json`); only new posts remain. With `--with-price` new posts' bodies are opened to extract a price, cached in `.price_cache.json` (max 6 per run; beyond that the price is marked "not fetched (over per-run cap)").
5. Periodic runs: fold them into your scheduler (e.g. one cron job iterating every board in config.json); the watcher itself only prints JSON and exits quietly when nothing is new.
6. Body fetch: mobile article URL first; on failure fall back to the PC `iframe[name=cafe_main]` (the PC top-level frame is the sidebar, not the body). Some posts return empty on both paths — record that as a fetch failure instead of estimating.

## Function 4 — Body & photo collection (collect.py)

```
python3 scripts/collect.py --cafe <cafe-domain> --club-id <club-id> \
  (--article <article-id> | --board <board-id> --limit N) [--no-images]
```

- Read-only. Collects body text + photos for one article, or the latest N articles of a board.
- Output: `~/data/naver-cafe/collect/<club-id>/<article-id>/<article-id>.md` + `images/` (001.jpg…) + `images/manifest.csv` (file · source_url · bytes). Icon-sized images (< 1 KB) are skipped.
- Photos come from the article page's `pstatic.net` images (including lazy `data-lazy-src`), saved at their original URLs.
- **Status (2026-09-27): live-verified** — single article (title · body · 4 images) and board batch (2 articles). Title from `h2.tit` (notice prefix stripped) → document.title fallback; body scoped to `div.post_cont`; images scoped to post_cont (profile thumbnails excluded).

## Board targeting knowledge

- Board-map extraction: pull `menuid=` + anchor text from the PC-UA desktop HTML (EUC-KR decode); `setup_watch.py` does this automatically. Mobile pages are JS-walled, so inspect structure through the desktop HTML route.
- Sale boards: 팝니다 · 삽니다 · 안전거래 · 분양 · Store sections are trading boards; spec/reference boards carry information posts — don't confuse the two.
- The Naver cafe-article search API returns public posts only and cannot target one cafe — unsuitable for scoped monitoring.

## Pitfalls

- Unauthenticated list APIs are blocked (`errorCode 9999`), and the legacy `ArticleList.nhn` redirects to `/f-e/` — never run without a cookie; always use the real-browser path.
- Cafe membership / board access is required — a cookie alone shows no posts otherwise. Check permissions first.
- The cookie is a login key: never share it or commit it; keep it in the server `.env` only. On expiry you get 9999 / login walls — issue a new one (no auto-refresh).
- The comment API returns the oldest 100 comments without pagination — verify the latest comments (sold-out etc.) directly in the UI (verified 2026-09-18).
- Don't over-call (pace at a few seconds per article). Collected content is copyrighted by its author/Naver — restrict use to internal analysis and alerts.
- Writing posts/comments carries macro-detection risk — always review the draft and keep frequency low.

## Verification

- Monitoring ✅ (2026-09-27): live sale-board fetch — 92 posts fetched, 1 new post detected inside the lookback window; the scheduled wrapper ran cleanly (exit 0).
- Collection ✅: single article (title · body · 4 images) + board batch (2 articles).
- Comment list ✅: 10 of 16 declared comments, author/text/time correct.
- Post writing / comment writing: live register test left to the operator (target + approval) — review screenshot → register → confirm the live post/comment.

## Cookie registration (hand this to a first-time user)

1. Log in to naver.com in desktop Chrome (or Edge) — complete 2FA.
2. Go to cafe.naver.com.
3. Press F12 — the developer panel opens.
4. Click **Application** in the panel tabs (find it behind » if hidden).
5. Expand **Storage → Cookies → https://cafe.naver.com**.
6. In the Name column, find **NID_AUT**; double-click its Value, select all, copy (Ctrl+C).
7. Copy **NID_SES** the same way — it looks multi-line but is a single value; copy it fully, don't truncate.
8. Register the pair by running `scripts/register_cookie.py` and pasting at the prompts — with `--env <path>` it updates that environment file in place (mode 600); without it, the ready-to-paste `NAVER_COOKIE=…` line is printed. Never put these values on a command line or into a repository.
9. Verify with one live board fetch; if it fails, re-copy the cookies (logging out invalidates them).
