---
name: naver-blog-manager
description: "Collect and manage Naver blogs: posts, comments, images."
version: 0.1.0
author: OpenIT (openit-ai), Hermes Agent
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [naver, blog, playwright, collection, images, korea]
    related_skills: [naver-cafe-manager]
---

# Naver Blog Manager

Run a Naver blog from one skill — four standard functions: **① post writing, ② comment check & reply, ③ new-post monitoring & alerts, ④ article text + photo collection**.

## Standard management functions

| # | Function | Tool | Default policy |
|---|---|---|---|
| 1 | Post writing | `scripts/post.py` | Fill the draft + screenshot by default; publishing requires explicit `--submit` (private by default) |
| 2 | Comment check & reply | `scripts/comments.py` | Read the list; composing stays a draft unless `--submit` |
| 3 | New-post monitoring & alerts | `scripts/watch.py` | New posts only on stdout; silent on none (cron-friendly) |
| 4 | Article text + photo collection | `scripts/naver-blog-collect.sh` pipeline | md + images (zip) + manifest; read-only |

## When to Use

- Collecting a blog's latest N / all posts (article text as md + original photos), delivering collected output (md + zip).
- Watching a blog for new posts / alerts; writing blog posts; managing comment replies.
- Don't use for: Naver Cafe work (→ `naver-cafe-manager`), keyword-search-based collection (prefer the official API HUB route).

## Prerequisites

- Collection pipeline: Node 18+ with `cheerio`. The skill is self-contained — one-time `(cd scripts && npm i cheerio)`.
- Post writing / comments (Playwright): `pip install playwright && playwright install chromium`; run the scripts with `python3`. If the browser binary lives elsewhere, override `CHROME_PATH` (honored by `post.py` and `comments.py`).
- Auth: reading works without login. Writing posts and registering comments need a logged-in session — `NAVER_COOKIE` in the server environment (registration procedure is the same as the cafe-manager skill's cookie guide). Never print or store the cookie.
- Paths: collection output `~/data/naver-blog/<blog-id>/`, drafts/screenshots `~/.hermes/data/naver-blog/manage/`, watch config/state `~/.hermes/data/naver-blog-manager/`.

## Function 1 — post writing (post.py)

```
python3 scripts/post.py \
  --blog <blog-id> --title "TITLE" --content-file body.md \
  [--images a.jpg,b.png] [--category CATEGORY] [--visibility private|public] [--submit]
```

1. Default is **draft mode**: open the editor (SE ONE) → fill title/body/images → save `draft_*.png` → exit (no publish).
2. With `--submit` it publishes from the publish layer using `--visibility` (default private). Publishing cannot be undone — review the screenshot first and keep it low-frequency.
3. If editor entry fails, pass `--editor-url` to enter the editor URL directly.
4. **Verification state (2026-09-27): structure implemented — one live publish test remains, run by the operator** (a target blog must be designated).

## Function 2 — comment check & reply (comments.py)

```
# List (read-only)
python3 scripts/comments.py --blog <blog-id> --log-no <log-no> --list
# Write (draft by default; --submit registers)
python3 scripts/comments.py --blog <blog-id> --log-no <log-no> --message "TEXT" [--reply-index N --submit]
```

1. Flow (measured 2026-09-27): mobile post page → click the comments button → CBOX loads → flat extraction of `li.u_cbox_comment` (author `.u_cbox_nick`, text `.u_cbox_contents`, time `.u_cbox_date`). Nested replies live inside the parent item's `.u_cbox_reply_area` — flagged `is_reply`. Secret comments are flagged `secret`; the blog-owner badge is flagged `is_owner`.
2. Writing: type into the contenteditable `.u_cbox_text` under `.u_cbox_write` → screenshot; `--submit` clicks the register button. `--reply-index` first tries the reply button on that item.
3. **Verification state (2026-09-27): list = verified live (a public post with comments — 10/10, count matched, secret/reply/owner distinguished); write/register = structure implemented, live test left to the operator.**

## Function 3 — new-post monitoring & alerts (watch.py)

```
python3 scripts/watch.py [--blog ID]... [--count N] [--json]
# config: ~/.hermes/data/naver-blog-manager/watch.json  {"blogs": [...], "count": 10}
```

1. Polls the `PostTitleListAsync.naver` list and diffs against state (`state.json`) — only new posts go to stdout (empty output → no cron alert).
2. The first run for a blog **saves a baseline and alerts nothing** (prevents a flood of old posts).
3. Fold periodic runs into your existing scheduler (avoid registering parallel crons for the same blog). Alert delivery is the caller's job (e.g. a Telegram delivery helper).
4. **Verification state (2026-09-27): verified live** — baseline of 10 posts saved + second run reported 0 new (JSON mode).

## Function 4 — article text + photo collection (collect pipeline)

```
bash scripts/naver-blog-collect.sh <blog-id> [count] [--mode both|md|zip] [--split MB]
```

| Request | Option | Delivered |
|---|---|---|
| Body only | `--mode md` | one md (images skipped → fast) |
| Images only (single zip) | `--mode zip` | one zip (images/ + manifest.csv) |
| Images only (20 MB split) | `--mode zip --split 20` | several partNN.zip parts |
| Body + images (default) | `--mode both` | md + full zip (local-link body md at the zip root) |

- Pass the stdout `MD=<path>` / `ZIP=<path>` lines through as-is. Split parts are independent zips — safe with the default Windows/macOS extractors.
- Delivery convention: **send the md plus a zip with the matching base filename** (the image bundle).

### Image-original rules (measured)

- `mblogthumb-phinf` URLs in the collected md are downscaled — swap the domain to `postfiles.pstatic.net` and append `?type=w3840`; `blogfiles.pstatic.net` with no query is also original. **Never use bare `postfiles` URLs (100px thumbnail).**
- `download-images.py`: fallback chain postfiles-w3840 → blogfiles-orig → source-w966; auto JPG/PNG/GIF detection with dimensions recorded (manifest.csv). On burst failures, 3 consecutive failures trigger a 30s cooldown that self-recovers.

### Parser pitfalls (measured fixes)

- The SE ONE paragraph unit is `.se-text-paragraph` only — also matching the module wrapper (`.se-module-text`) duplicates every sentence.
- Posts younger than 24h expose only a relative timestamp ("N hours ago") on Naver — the absolute meta is absent; the collector annotates an `≈` approximation relative to collection time.
- The list API supports `countPerPage` (latest N = `currentPage=1&countPerPage=N`).

### Full-collection procedure (proven)

1. **Establish the total post count first** — walk months via `viewdate`: `PostTitleListAsync.naver?blogId=<id>&viewdate=<YYYY-MM>&currentPage=N&countPerPage=30` — scan back to the blog's opening month (one empty month ends the walk).
2. For hundreds+ posts, collect incrementally with an archiver tool (→ tools below); small blogs are collected in full by the default pipeline.
3. If images are expected to reach multi-GB, report scale/estimated size before starting.

### Output & verification record

- (2026-09-14) latest 10 posts of a test blog: MD 55KB, images 73/73 succeeded (82MB, widths 620–3840, JPG 59 + PNG 14, PNG originals preserved).
- Split test: 73 images / 80MB → `--split 20` produced 4 parts (19.6–19.99MB each), 0 missing/duplicated, all parts integrity-checked.

## Endpoint facts (measured)

- List: `blog.naver.com/PostTitleListAsync.naver?blogId=<id>&countPerPage=10&currentPage=<n>` — works. Non-standard JSON (`postList`). The `m.blog` variant returns empty.
- RSS (`blog.naver.com/<id>/rss`) is **dead (404)** — RSS-based tools are not usable.
- Body: `m.blog.naver.com/<id>/<log-no>` — 200 OK (parses SmartEditor ONE and the legacy editor). No server-IP blocking.
- Alternative tools (README-based, not executed here — reference only): hubert-bioinformatics/naver-blog-archiver (MIT — incremental JSONL, high confidence), lazyyoyo/naver-blog-importer (MIT), jeongseup/naver-blog-backer (GPL-3.0), isnow890/naver-search-mcp (MIT — search API MCP). No large project covers this space — verify before relying.
- The Search OpenAPI moved to NAVER API HUB (NCP) — new sign-ups closed 2026-07-31, existing keys end 2027-06-30 (source: developers.naver.com/notice/article/32530).

## Pitfalls

- **The image directory is wiped before every run** — leftovers would mix into the zip and desync the manifest (the pipeline does this automatically).
- Body links inside the zip are relative to `images/<file>` — keep the body md at the zip **root** (inside `images/` it breaks into `images/images/`).
- **Never read the visitor counter as a post count** — a blog home's `today N / total M` is a visitor counter (real case: 423 misread vs 9 actual posts). Post-count evidence: monthly viewdate scan totals and list API returns.
- Keep delays during bulk collection (anti-bot). Rights to collected content belong to the publisher/Naver — use for internal analysis only.
- Post writing and comments risk macro detection — review drafts and keep frequency low.
- Blog comment APIs (`m.blog.naver.com/api/.../comments-info`, `apis.naver.com/commentBox/...`) return **403 for plain HTTP calls outside a browser context** — comment work goes through Playwright only. Over-calling in a short window can block temporarily; stay low-frequency.
- Delivery precondition: large zips need a raised file limit on the delivery channel (e.g. a local Bot API server raises Telegram's limit to 2GB — verified with 80MB bundles).

## Verification

- Collection ✅ (2026-09-27): latest 2 posts — `MD=` 12KB with original-resolution image URLs (full run: 73/73 images, see record above).
- Monitoring ✅: baseline 10 posts → second run 0 new + `state.json` refreshed.
- Comment list ✅: public post with comments — 10/10, count matched.
- Post writing / comment registration: live registration test left to the operator (target designated) — review the screenshot → register → confirm on the live site.
