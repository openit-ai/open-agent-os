#!/usr/bin/env python3
"""Naver cafe article collector — body text + photos (read-only).

Usage:
  # single article
  python3 collect.py \
      --cafe <cafe-domain> --club-id <club-id> --article <article-id> [--no-images]
  # latest N articles from a board
  python3 collect.py --cafe <cafe-domain> --club-id <club-id> \
      --board <board-id> --limit 3 [--no-images]

DOM notes (measured 2026-09-27 on the mobile article page):
- Title: h2.tit (notice prefix stripped) -> document.title fallback
  (Naver site-name suffix removed)
- Body: div.post_cont (falls back to body)
- Photos: img/source inside div.post_cont (pstatic.net); profile
  thumbnails (c77_) excluded
Output : JSON {status, articles:[{article_id, title, md, images, bytes_ok, failed}], outdir}
Files  : <outdir>/<club-id>/<article_id>/<article_id>.md, images/*, images/manifest.csv
Auth   : NAVER_COOKIE env (optional — public posts are readable logged out)
"""
import argparse
import csv
import json
import os
import re
import sys
import time

UA_MOBILE = ("Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")
UA_PC = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

IMG_JS = """
els => els.map(e => e.getAttribute('data-lazy-src') || e.getAttribute('src')
           || (e.getAttribute('srcset') || '').split(',')[0].trim().split(' ')[0] || '')
           .filter(Boolean)"""

# Naver UI text (functional — matched against live pages)
TITLE_JS = """() => {
  const h = document.querySelector('h2.tit');
  if (h) {
    const t = h.innerText.split('\\n').map(x => x.trim()).filter(Boolean);
    if (t[0] === '공지' && t.length > 1) t.shift();
    if (t.length) return t.join(' ');
  }
  return '';
}"""


def out(p):
    print(json.dumps(p, ensure_ascii=False))


def die(step, msg):
    out({"status": "error", "step": step, "message": msg})
    sys.exit(1)


def ext_of(data: bytes, ctype: str) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"GIF":
        return ".gif"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if "png" in ctype:
        return ".png"
    if "gif" in ctype:
        return ".gif"
    if "webp" in ctype:
        return ".webp"
    return ".jpg"


def keep_img(url: str) -> bool:
    u = url.lower()
    if "pstatic.net" not in u:
        return False
    if "/static/" in u or "blank" in u or "icon" in u:
        return False
    if "c77_" in u or "thumbnail" in u or "profile" in u:
        return False
    return True


def fetch_article(ctx, cafe, club_id, aid):
    """(title, body_text, image_urls, final_url) — mobile first, PC frame fallback."""
    page = ctx.new_page()
    mob = f"https://m.cafe.naver.com/ca-fe/web/cafes/{club_id}/articles/{aid}"
    title, body, imgs = "", "", []
    try:
        page.goto(mob, timeout=45000)
        page.wait_for_timeout(7000)
        try:
            title = (page.evaluate(TITLE_JS) or "").strip()
            if not title:
                # Naver UI text (functional — matched against live pages)
                title = (page.title() or "").replace(" : 네이버 카페", "").strip()
        except Exception:
            title = ""
        try:
            el = page.locator("div.post_cont").first
            if el.count():
                body = el.inner_text()
        except Exception:
            body = ""
        if not (body or "").strip():
            body = page.inner_text("body")
        for sel in ("div.post_cont img, div.post_cont source", "img"):
            try:
                urls = page.eval_on_selector_all(sel, IMG_JS)
                imgs = [u for u in urls if keep_img(u)]
                if imgs:
                    break
            except Exception:
                continue
        if len((body or "").strip()) > 300:
            return title, body, imgs, page.url
    except Exception:
        pass
    # PC fallback — the cafe_main iframe
    try:
        pc = ctx.new_page()
        pc.goto(f"https://cafe.naver.com/{cafe}/{aid}", timeout=45000)
        for _ in range(10):
            pc.wait_for_timeout(2000)
            for f in pc.frames:
                if (f.name or "") == "cafe_main":
                    tb = f.inner_text("body")
                    if tb and len(tb.strip()) > 200:
                        fi = [u for u in f.eval_on_selector_all("img", IMG_JS)
                              if keep_img(u)]
                        if not title:
                            # Naver UI text (functional — matched against live pages)
                            try:
                                title = (f.evaluate(
                                    "() => document.title"
                                    ".replace(' : 네이버 카페', '').trim()") or "").strip()
                            except Exception:
                                pass
                        return title, tb, fi or imgs, pc.url
    except Exception:
        pass
    return title, body, imgs, mob


def fetch_board_ids(ctx, club_id, menu_id, limit):
    page = ctx.new_page()
    url = (f"https://cafe.naver.com/f-e/cafes/{club_id}/menus/"
           f"{menu_id}?viewType=L")
    page.goto(url, timeout=45000)
    page.wait_for_timeout(6000)
    items = page.eval_on_selector_all(
        "a[href*='/articles/']",
        """els => els.map(e => ({h: e.getAttribute('href') || '',
                                 t: e.innerText.trim(),
                                 ctx: (e.closest('li,tr,div')||{innerText:''}).innerText}))""")
    ids, seen = [], set()
    for it in items:
        m = re.search(r"/articles/(\d+)", it.get("h") or "")
        if not m or "commentFocus" in (it.get("h") or ""):
            continue
        aid = m.group(1)
        if aid in seen:
            continue
        # Naver UI text (functional — matched against live pages: notice prefix)
        if "공지" in (it.get("ctx") or "")[:40]:
            continue
        seen.add(aid)
        ids.append(aid)
        if len(ids) >= limit:
            break
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cafe", required=True)
    ap.add_argument("--club-id", required=True)
    ap.add_argument("--article", default="", help="article id or URL")
    ap.add_argument("--board", default="", help="board menuid (latest N)")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--outdir", default=os.path.expanduser(
        "~/data/naver-cafe/collect"))
    a = ap.parse_args()
    if not a.article and not a.board:
        die("init", "--article or --board is required")

    cookie = os.environ.get("NAVER_COOKIE")
    ck = []
    if cookie:
        parts = dict(p.split("=", 1) for p in cookie.split(";") if "=" in p)
        ck = [{"name": k.strip(), "value": v.strip(), "domain": ".naver.com",
               "path": "/"} for k, v in parts.items()]

    from playwright.sync_api import sync_playwright
    chrome = (os.environ.get("CHROME_PATH") or os.path.expanduser(
        "~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"))
    results = []
    with sync_playwright() as pw:
        kw = {"headless": True,
              "args": ["--no-sandbox", "--disable-gpu",
                       "--disable-dev-shm-usage"]}
        if os.path.exists(chrome):
            kw["executable_path"] = chrome
        b = pw.chromium.launch(**kw)
        ctx = b.new_context(user_agent=UA_MOBILE)
        if ck:
            ctx.add_cookies(ck)

        if a.article:
            if a.article.isdigit():
                aids = [a.article]
            else:
                m = re.search(r"/(\d+)(?:[?#]|$)", a.article)
                aids = [m.group(1)] if m else []
        else:
            aids = fetch_board_ids(ctx, a.club_id, a.board, a.limit)

        for aid in aids:
            adir = os.path.join(a.outdir, a.club_id, aid)
            os.makedirs(adir, exist_ok=True)
            title, body, imgs, src_url = fetch_article(
                ctx, a.cafe, a.club_id, aid)
            saved, manifest = [], []
            if not a.no_images and imgs:
                idir = os.path.join(adir, "images")
                os.makedirs(idir, exist_ok=True)
                for i, u in enumerate(imgs, 1):
                    try:
                        r = ctx.request.get(u, timeout=30000)
                        if r.status != 200:
                            continue
                        data = r.body()
                        if len(data) < 1000:  # skip icon-sized images
                            continue
                        ext = ext_of(data, (r.headers.get("content-type") or ""))
                        fn = f"{i:03d}{ext}"
                        with open(os.path.join(idir, fn), "wb") as fh:
                            fh.write(data)
                        saved.append(fn)
                        manifest.append([fn, u, len(data)])
                    except Exception:
                        continue
                with open(os.path.join(idir, "manifest.csv"), "w",
                          newline="") as fh:
                    w = csv.writer(fh)
                    w.writerow(["file", "source_url", "bytes"])
                    w.writerows(manifest)
            md = []
            md.append(f"# {title or '(title not detected)'}")
            md.append("")
            md.append(f"- Source: https://cafe.naver.com/{a.cafe}/{aid}")
            md.append(f"- Cafe: {a.cafe} (clubid {a.club_id})")
            md.append(f"- Collected: {time.strftime('%Y-%m-%d %H:%M KST')}")
            md.append("")
            md.append((body or "").strip() or "_(body fetch failed)_")
            if saved:
                md.append("")
                md.append("## Photos")
                for fn in saved:
                    md.append(f"![](images/{fn})")
            md_path = os.path.join(adir, f"{aid}.md")
            with open(md_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(md) + "\n")
            results.append({"article_id": aid, "title": title,
                            "md": md_path, "images": len(saved),
                            "bytes_ok": sum(x[2] for x in manifest),
                            "failed": not bool((body or "").strip())})
            time.sleep(2)
        b.close()
    out({"status": "ok", "outdir": a.outdir, "articles": results})


if __name__ == "__main__":
    main()
