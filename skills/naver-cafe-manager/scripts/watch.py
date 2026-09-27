#!/usr/bin/env python3
"""Naver cafe board watcher (Playwright backend).

Run with: python3 watch.py ...
Auth: NAVER_COOKIE env ("NID_AUT=..; NID_SES=..") — server .env only.
Output: JSON {board, fetched, matched, new[]} to stdout.
State: ~/.hermes/data/naver-cafe-watch/<club>_<menu>.json
Notify (Telegram) is done by the caller, not here.
"""
import argparse
import json
import os
import re
import sys
import time

CHROME = (os.environ.get("CHROME_PATH") or os.path.expanduser(
    "~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")
STATE_DIR = os.path.expanduser("~/.hermes/data/naver-cafe-watch")
PRICE_CACHE = os.path.join(STATE_DIR, ".price_cache.json")
MAX_PRICE_FETCH = 6  # cap on price lookups per run
# Naver UI text (functional — matched against live pages)
SOLD_PATTERNS = re.compile(
    r"판매완료|분양완료|거래완료|마감됨|품절|sold\s*out", re.I)
# Posted date in list-row text: 2026.09.18. / 2026.09.18
DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})\.?")
# Naver UI text (functional — matched against live pages) — same-day posts
# show a clock time (Korean AM/PM prefix) instead of a date.
TIME_RE = re.compile(r"\b(?:오전|오후)?\s*\d{1,2}:\d{2}\b")


def parse_post_date(ctx: str, today=None):
    """Extract the posted date from list-row text. None when not found."""
    import datetime as _dt
    today = today or _dt.date.today()
    m = DATE_RE.search(ctx or "")
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if TIME_RE.search(ctx or ""):
        return today  # time-only listing = posted today
    return None


# Naver UI text (functional — matched against live pages)
# Price tags (per-unit KRW / plain KRW / ten-thousand-KRW / ₩), and
# "price on request" hints.
PRICE_RE = re.compile(
    r"(개당\s*[\d,]+\s*원|[\d,]{3,}\s*원|\d+\s*만\s*원|₩\s*[\d,]+)")
NO_PRICE_HINTS = re.compile(r"(B\s*TO\s*B|비공개|문의\s*바랍|가격\s*문의)", re.I)


def extract_price(text: str) -> str:
    """Extract a price tag from body text. "" when none found."""
    if not text:
        return ""
    cands = []
    for ln in text.splitlines():
        ln = re.sub(r"\s+", " ", ln).strip()
        if not ln or len(ln) > 120:
            continue
        m = PRICE_RE.search(ln)
        if m:
            cands.append(ln)
    if cands:
        cands.sort(key=lambda s: (len(s), s))
        return cands[0][:90]
    for ln in text.splitlines():
        ln = re.sub(r"\s+", " ", ln).strip()
        if NO_PRICE_HINTS.search(ln):
            return "price on request (B2B note)"
    return ""


def _price_cache_load() -> dict:
    try:
        return json.load(open(PRICE_CACHE))
    except Exception:
        return {}


def _price_cache_save(c: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        json.dump(c, open(PRICE_CACHE, "w"), ensure_ascii=False)
    except Exception:
        pass


def fetch_article_text(cafe, club_id, aid, cookie) -> str:
    """Article body text. Mobile first, PC (cafe_main frame) fallback."""
    from playwright.sync_api import sync_playwright
    parts = dict(p.split("=", 1) for p in cookie.split(";") if "=" in p)
    ck = [{"name": k.strip(), "value": v.strip(),
           "domain": ".naver.com", "path": "/"} for k, v in parts.items()]
    mob = f"https://m.cafe.naver.com/ca-fe/web/cafes/{club_id}/articles/{aid}"
    with sync_playwright() as p:
        kw = dict(headless=True, args=["--no-sandbox", "--disable-gpu",
                                       "--disable-dev-shm-usage"])
        if os.path.exists(CHROME):
            kw["executable_path"] = CHROME
        b = p.chromium.launch(**kw)
        try:
            ctx = b.new_context(user_agent=UA_MOBILE)
            ctx.add_cookies(ck)
            pg = ctx.new_page()
            pg.goto(mob, timeout=40000)
            pg.wait_for_timeout(8000)
            t = pg.inner_text("body")
            if t and len(t.strip()) > 300:
                return t
            ctx.close()
            ctx = b.new_context(user_agent=UA)
            ctx.add_cookies(ck)
            pg = ctx.new_page()
            pg.goto(f"https://cafe.naver.com/{cafe}/{aid}", timeout=40000)
            for _ in range(8):
                pg.wait_for_timeout(2000)
                for f in pg.frames:
                    if (f.name or "") == "cafe_main":
                        try:
                            tb = f.inner_text("body")
                        except Exception:
                            continue
                        if tb and len(tb.strip()) > 200:
                            return tb
        except Exception:
            return ""
        finally:
            b.close()
    return ""


def fetch_board(cafe, club_id, menu_id, cookie):
    from playwright.sync_api import sync_playwright
    parts = dict(p.split("=", 1) for p in cookie.split(";") if "=" in p)
    cookies = [{"name": k.strip(), "value": v.strip(),
                "domain": ".naver.com", "path": "/"}
               for k, v in parts.items()]
    url = (f"https://cafe.naver.com/f-e/cafes/{club_id}/menus/"
           f"{menu_id}?viewType=L")
    with sync_playwright() as p:
        kw = dict(headless=True, args=["--no-sandbox", "--disable-gpu",
                                       "--disable-dev-shm-usage"])
        if os.path.exists(CHROME):
            kw["executable_path"] = CHROME
        b = p.chromium.launch(**kw)
        ctx = b.new_context(user_agent=UA)
        ctx.add_cookies(cookies)
        pg = ctx.new_page()
        pg.goto(url, timeout=30000)
        pg.wait_for_timeout(6000)
        # Naver UI text (functional — matched against live pages)
        if "nidlogin" in pg.url or "로그인" in pg.title():
            b.close()
            sys.exit("auth-or-block: login wall (check cookie)")
        items = pg.eval_on_selector_all(
            'a[href*="/articles/"]',
            """els => els.map(e => {
                 let li = e.closest('li, tr, div[class*=item], div[class*=Article]');
                 return {t: e.innerText, h: e.getAttribute('href'),
                         ctx: li ? li.innerText.replace(/\\n+/g,' ') : ''};
               })""")
        b.close()
    posts, seen = [], set()
    for it in items:
        href = it.get("h") or ""
        if "commentFocus" in href:
            continue
        m = re.search(r"/articles/(\d+)", href)
        if not m:
            continue
        aid = m.group(1)
        if aid in seen:
            continue
        seen.add(aid)
        title = re.sub(r"\s+", " ", it.get("t") or "").strip()
        # Naver UI text (functional — matched against live pages: comment-count label)
        if not title or title.startswith("댓글수"):
            continue
        d = parse_post_date(it.get("ctx") or "")
        posts.append({
            "article_id": aid,
            "title": title,
            "url": f"https://cafe.naver.com/{cafe}/{aid}",
            "posted": d.isoformat() if d else "",
        })
    return posts


def main():
    import datetime as _dt
    ap = argparse.ArgumentParser()
    ap.add_argument("--cafe", required=True)
    ap.add_argument("--club-id", required=True)
    ap.add_argument("--board", required=True)
    ap.add_argument("--keywords", default="21700,18650")
    ap.add_argument("--all", action="store_true",
                    help="ignore keywords; every new post except sold-out ones")
    ap.add_argument("--max-age-days", type=int, default=31,
                    help="only posts from the last N days (0 = no limit)")
    ap.add_argument("--with-price", action="store_true",
                    help="open new articles and extract a price")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--report-file", default=None)
    ap.add_argument("--no-state", action="store_true")
    a = ap.parse_args()
    cookie = os.environ.get("NAVER_COOKIE")
    if not cookie:
        sys.exit("missing NAVER_COOKIE in server .env")
    kws = [k.strip().lower() for k in a.keywords.split(",") if k.strip()]
    state_dir = os.path.expanduser(a.state_dir) if a.state_dir else STATE_DIR
    posts = fetch_board(a.cafe, a.club_id, a.board, cookie)
    time.sleep(1)
    # Posted-date window filter — never surface stale posts as live listings.
    today = _dt.date.today()
    if a.max_age_days and a.max_age_days > 0:
        cut = today - _dt.timedelta(days=a.max_age_days)
        pool = [p for p in posts if p["posted"]
                and _dt.date.fromisoformat(p["posted"]) >= cut]
        stale = [p for p in posts if p["posted"]
                 and _dt.date.fromisoformat(p["posted"]) < cut]
        undated = [p for p in posts if not p["posted"]]
    else:
        pool, stale, undated = posts, [], []
    if a.all:
        matched = [p for p in pool
                   if not SOLD_PATTERNS.search(p["title"])]
    else:
        matched = [p for p in pool
                   if any(k in p["title"].lower() for k in kws)
                   and not SOLD_PATTERNS.search(p["title"])]
    new = matched
    if not a.no_state:
        os.makedirs(state_dir, exist_ok=True)
        sp = os.path.join(state_dir, f"{a.club_id}_{a.board}.json")
        old = set(json.load(open(sp)) if os.path.exists(sp) else [])
        new = [p for p in matched if p["article_id"] not in old]
        json.dump(sorted(old | {p["article_id"] for p in matched}),
                  open(sp, "w"))
    # New items: open the body and attach a price (--with-price).
    if a.with_price and new:
        cache = _price_cache_load()
        dirty = False
        for p in new[:MAX_PRICE_FETCH]:
            aid = p["article_id"]
            if aid in cache:
                p["price"] = cache[aid]
                continue
            body = fetch_article_text(a.cafe, a.club_id, aid, cookie)
            p["price"] = (extract_price(body) if body
                          else "unavailable (body fetch failed)")
            if body and not p["price"]:
                p["price"] = "not listed (no price in body)"
            cache[aid] = p["price"]
            dirty = True
            time.sleep(1)
        if dirty:
            _price_cache_save(cache)
        for p in new[MAX_PRICE_FETCH:]:
            p["price"] = "not fetched (over per-run cap)"
    print(json.dumps({"board": a.board, "fetched": len(posts),
                      "in_window": len(pool), "stale_skipped": len(stale),
                      "undated_skipped": len(undated),
                      "matched": len(matched), "new": new},
                     ensure_ascii=False))
    if a.report_file and new:
        with open(os.path.expanduser(a.report_file), "a") as f:
            for p in new:
                f.write(f"- [{p['posted']}] {p['title']} — {p['url']}\n")


if __name__ == "__main__":
    main()
