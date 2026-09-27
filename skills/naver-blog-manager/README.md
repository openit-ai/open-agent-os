# naver-blog-manager — skill guide

> Collect and manage Naver blogs from a chat request — write posts, check & reply to comments, watch for new posts, and collect article text plus original-resolution photos.
> This document records how the skill was built, what has been verified, and how to run it step by step.

- **Version**: 0.1.0 · **Platform**: Linux (Hermes Agent)
- **Location (installed)**: `~/.hermes/skills/naver-blog-manager/`

**How to read this document**
- What it does → §1 · Structure → §2 · Background → §3
- Prerequisites → §4
- Verified results → §5 · Step-by-step usage → §6 · Troubleshooting → §7

---

## 1. What it does

One skill covering four standard Naver blog functions:

| # | Function | Tool | Role |
|---|---|---|---|
| 1 | Post writing | `scripts/post.py` | Draft-first editor fill (SE ONE) + screenshot; publish only with `--submit` |
| 2 | Comment check & reply | `scripts/comments.py` | Flat CBOX comment list; draft-first replies via Playwright |
| 3 | New-post monitoring & alerts | `scripts/watch.py` | Baseline-then-diff over the post list; cron-friendly, silent when nothing is new |
| 4 | Article text + photo collection | `scripts/naver-blog-collect.sh` | Body md from live pages + original-resolution images + manifest; zip delivery (optional split) |

**Design rules**

1. **Read without login, write with a session** — collection and comment listing are anonymous; writing needs `NAVER_COOKIE`.
2. **Draft-first writes** — post writing fills the editor and screenshots; nothing is published or registered unless `--submit` is passed.
3. **Original resolution or nothing** — image URLs are rewritten to original-resolution endpoints; bare Naver thumbnail URLs are never used.
4. **Alert on new only** — the first monitoring run stores a baseline silently; later runs print only fresh posts.
5. **Secrets stay server-side** — the cookie is read from the environment and is never printed or stored by the scripts.

## 2. Structure

```
naver-blog-manager/
├── SKILL.md                     # the runbook Hermes loads
├── README.md                    # this document
└── scripts/
    ├── naver-blog-collect.sh    # pipeline entry: collect → download → localize → zip (both|md|zip, --split)
    ├── collect-latest.mjs       # latest N posts → markdown + JSONL (cheerio; parser based on naver-blog-archiver, MIT)
    ├── download-images.py       # md image URLs → original-resolution files + manifest.csv (fallback chain, cooldown)
    ├── localize-md.py           # rewrite md links to zip-local images/<file> paths (<name>-body.md)
    ├── pack-images.py           # greedy split into partNN.zip under a size limit
    ├── watch.py                 # new-post monitor (state diff, stdout-only alerts)
    ├── post.py                  # draft-first post writer (Playwright, SE ONE)
    ├── comments.py              # comment list / reply (Playwright, mobile CBOX)
    └── package.json             # cheerio dependency for the Node collector
```

## 3. Background

- **No official blog API.** The skill is built on the public post-list endpoint and the mobile post pages, measured live (2026-09-27):
  - List: `blog.naver.com/PostTitleListAsync.naver?blogId=<id>&countPerPage=10&currentPage=<n>` — works; non-standard JSON (`postList`). The `m.blog` variant returns empty.
  - RSS (`blog.naver.com/<id>/rss`) is **dead (404)** — RSS-based tooling is not usable.
  - Body: `m.blog.naver.com/<id>/<log-no>` — 200 OK, parses both SmartEditor ONE and the legacy editor; no server-IP blocking observed.
- **Parser lineage.** `collect-latest.mjs` is a custom parser adapted from [hubert-bioinformatics/naver-blog-archiver](https://github.com/hubert-bioinformatics/naver-blog-archiver) (MIT).
- **Comments via mobile CBOX.** The comment panel on the mobile post page renders the CBOX widget; the script clicks the comments button, waits for `.u_cbox`, then extracts `li.u_cbox_comment` items flat (author/text/time), flagging replies, secret comments and the blog-owner badge.
- **Comment APIs are browser-only.** `m.blog.naver.com/api/.../comments-info` and `apis.naver.com/commentBox/...` return **403 for plain HTTP calls outside a browser context** — comment work must run through Playwright.
- **Search OpenAPI relocation.** Naver's Search OpenAPI moved to NAVER API HUB (NCP): new sign-ups closed 2026-07-31, existing keys end 2027-06-30 (source: developers.naver.com/notice/article/32530).

## 4. Prerequisites

- **Node 18+** with `cheerio` (for `collect-latest.mjs`). The skill is self-contained: `(cd scripts && npm i cheerio)` once.
- **Python 3** with **Playwright** (for `post.py` / `comments.py`): `pip install playwright && playwright install chromium`. If the browser binary lives elsewhere, export `CHROME_PATH` (both scripts honor it).
- **Auth**: reading needs nothing. Writing posts and registering comments need a logged-in Naver session exported as `NAVER_COOKIE` (a `name=value; name2=value2` cookie header string) in the server environment — never in a repo or chat. Cookie registration follows the same procedure as the `naver-cafe-manager` skill.
- **Paths** (defaults, all overridable): collection output `~/data/naver-blog/<blog-id>/` (`OUT_DIR` env); drafts/screenshots `~/.hermes/data/naver-blog/manage/`; watch config/state `~/.hermes/data/naver-blog-manager/` (`--config` / `--state`).
- **Cost**: none beyond your machine; all traffic is plain HTTPS to Naver endpoints.

## 5. Verified results (2026-09-27)

- **Monitoring**: first run saved a baseline of the latest 10 posts with no alert; the second run reported 0 new posts (state file persisted) — baseline-then-diff behavior confirmed.
- **Collection (latest N)**: collection of the 2 latest posts produced a 12KB markdown file with original-resolution image URLs.
- **Collection (full run, earlier)**: 73/73 images fetched — 82MB total (JPG 59 + PNG 14; widths 620–3840; PNG originals preserved).
- **Zip split**: the split test produced 4 parts (~20MB each) with 0 missing and 0 duplicated images across parts.
- **Comments**: extraction on a public post returned 10/10 comments with the declared count matching, correctly flagging secret comments, nested replies, and the blog-owner badge.
- **Pitfall confirmed**: the CBOX comment APIs (`m.blog.naver.com/api/.../comments-info`, `apis.naver.com/commentBox/...`) return 403 outside a browser context — browser automation is required.
- **Writes are draft-first**: `post.py` fills the editor without publishing unless `--submit` is passed; live submit/registration testing is left to the operator.

## 6. Step-by-step usage

Install:

```bash
hermes skills install openit-ai/open-agent-os/skills/naver-blog-manager
```

Prepare dependencies:

```bash
(cd scripts && npm i cheerio)                 # Node collector
pip install playwright && playwright install chromium   # browser scripts
```

**Collection** — latest N posts (default `both` = md + full image zip):

```bash
bash scripts/naver-blog-collect.sh <blog-id> 10                 # md + zip
bash scripts/naver-blog-collect.sh <blog-id> 10 --mode md       # body only (fast)
bash scripts/naver-blog-collect.sh <blog-id> 10 --mode zip --split 20   # image zips ≤20MB
# stdout: MD=<path> / ZIP=<path> (machine-readable — pass along as-is)
```

**Monitoring** — feed the blog IDs once, then poll on a schedule:

```bash
python3 scripts/watch.py --blog <blog-id> --count 10            # first run: baseline (silent)
python3 scripts/watch.py --blog <blog-id> --count 10 --json     # later runs: new posts as JSON
# config file: ~/.hermes/data/naver-blog-manager/watch.json  {"blogs": ["<id>"], "count": 10}
```

**Post writing** — draft by default; publishing is explicit:

```bash
export NAVER_COOKIE='...'   # server-side env only
python3 scripts/post.py --blog <blog-id> --title "TITLE" --content-file body.md \
    [--images a.jpg,b.png] [--category CATEGORY] [--visibility private|public] [--submit]
```

**Comments** — list, then reply (draft-first):

```bash
python3 scripts/comments.py --blog <blog-id> --log-no <log-no> --list
python3 scripts/comments.py --blog <blog-id> --log-no <log-no> --message "TEXT" [--reply-index N --submit]
```

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `cheerio not installed` from the collect script | `npm i cheerio` never ran | `(cd scripts && npm i cheerio)` |
| Comment list returns 403 / empty over plain HTTP | CBOX APIs refuse non-browser contexts | run `comments.py` (Playwright), never call those APIs directly |
| Writer gets a login wall (`nidlogin` URL) | `NAVER_COOKIE` missing/expired | refresh the cookie in the server environment; re-run |
| Editor entry fails | SE ONE structure changed | pass `--editor-url` to enter directly, or adjust the selectors |
| Latest post shows "N hours ago", no timestamp | posts < 24h old have relative-only timestamps | use the collector's `≈` approximation (relative to collection time) |
| Images in the zip don't match the manifest | stale files from a previous run | the pipeline wipes `images/` automatically — don't run download steps out of order |
| ZIP links show `images/images/...` | body md placed inside `images/` | keep the body md at the zip root |
| Downloaded image is 100px | bare `postfiles.pstatic.net` URL (thumbnail) | always use `?type=w3840` (or `blogfiles` with no query) |
| Bulk download stalls then recovers | burst failure cooldown | expected: 3 consecutive failures → 30s cooldown, then resumes |
| Post count looks wrong | the blog home counter is *visitors*, not posts | count via monthly `viewdate` scan totals / list API |

## 8. References

- Naver blog (public pages used): https://blog.naver.com · https://m.blog.naver.com
- Post list endpoint: `https://blog.naver.com/PostTitleListAsync.naver?blogId=<id>&countPerPage=10&currentPage=1`
- Parser base: https://github.com/hubert-bioinformatics/naver-blog-archiver (MIT)
- Related tools (README-based, verify before use): https://github.com/lazyyoyo/naver-blog-importer (MIT) · https://github.com/jeongseup/naver-blog-backer (GPL-3.0) · https://github.com/isnow890/naver-search-mcp (MIT)
- Naver Search OpenAPI notice (API HUB migration): https://developers.naver.com/notice/article/32530
- Hermes skills docs: https://hermes-agent.nousresearch.com/docs
- In-skill: `SKILL.md` · `scripts/` (collect pipeline, watch, post, comments)

---
Part of the [open-agent-os](https://github.com/openit-ai/open-agent-os) repository · License: Apache-2.0
