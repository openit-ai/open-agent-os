#!/usr/bin/env python3
"""Naver cafe article comments — list and reply (draft-first, Playwright).

Usage:
  # comment list (read-only — also the default when no message is given)
  python3 comments.py \
      --cafe <cafe-domain> --club-id <club-id> --article <article-id> --list
  # write a comment (default: fill + screenshot only; --submit registers)
  python3 comments.py --cafe <cafe-domain> --club-id <club-id> \
      --article <article-id> --message "text" [--reply-index 2] [--submit]

DOM notes (measured 2026-09-27 on the mobile article page):
- List: ul.comment_list div.comment_item
  (author .nick_name .ellip, text .comment_content .txt, time .comment_footer .date)
- Writing: click a comment's reply button (button.btn_write) -> editor opens
  -> type -> submit (the top-level comment button varies by skin — looked up
  from the list)
Auth : NAVER_COOKIE env — server .env only.
Output: one-line JSON {status, count, comments[], screenshots[], message}
"""
import argparse
import json
import os
import re
import sys
import time

COMMENT_JS = """
els => els.map(e => {
  const q = (sel) => { const n = e.querySelector(sel); return n ? n.innerText.trim() : ''; };
  return {
    author: q('.nick_name .ellip') || q('.nick_name') || q('.u_cbox_nick'),
    text: q('.comment_content .txt') || q('.comment_content') || q('.u_cbox_contents'),
    time: q('.comment_footer .date') || q('.u_cbox_date'),
    isReply: !!(e.closest('.comment_reply')
                || String(e.className || '').includes('reply')),
    all: e.innerText.replace(/\\n+/g, ' | ').trim()
  };
})"""
LIST_SELECTORS = [
    "ul.comment_list div.comment_item",
    ".talk_comment_list div.comment_item",
    "div.comment_item",
    ".u_cbox_comment",
]
# Naver UI text (functional — matched against live pages)
WRITE_BUTTON_SELECTORS = [
    "button.btn_write",
    "button:has-text('댓글쓰기')",
    ".section_comment_btn",
    "a:has-text('댓글쓰기')",
    "[class*='comment_btn']",
]
INPUT_SELECTORS = [
    "div[class*='comment_write'] textarea",
    "textarea",
    "div[class*='comment_write'] [contenteditable='true']",
    "[contenteditable='true']",
]
# Naver UI text (functional — matched against live pages)
SUBMIT_SELECTORS = [
    "button:has-text('등록')",
    "button:has-text('등록하기')",
    ".btn_register",
    "a:has-text('등록')",
]
# Naver UI text (functional — matched against live pages)
COUNT_JS = """() => {
  const t = [...document.querySelectorAll('div,a,h3,span,strong')]
    .map(e => (e.innerText || '').trim())
    .find(t => /^댓글\\s*\\n?\\s*\\d+$/.test(t));
  return t || '';
}"""


def out(p):
    print(json.dumps(p, ensure_ascii=False))


def die(step, msg, shots=(), url=""):
    out({"status": "error", "step": step, "message": msg,
         "screenshots": list(shots), "editor_url": url})
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cafe", required=True)
    ap.add_argument("--club-id", required=True)
    ap.add_argument("--article", required=True,
                    help="article id or article URL")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--message", default="", help="comment text to write")
    ap.add_argument("--reply-index", type=int, default=-1,
                    help="0-based index of the comment to reply to")
    ap.add_argument("--submit", action="store_true",
                    help="actually submit (default: fill + screenshot only)")
    ap.add_argument("--outdir", default=os.path.expanduser(
        "~/.hermes/data/naver-cafe/manage"))
    a = ap.parse_args()
    if not a.message:
        a.list = True
    if a.article.isdigit():
        aid = a.article
    else:
        m = re.search(r"/(\d+)(?:[?#]|$)", a.article)
        aid = m.group(1) if m else ""
    if not aid:
        die("init", f"could not parse an article id from: {a.article}")

    cookie = os.environ.get("NAVER_COOKIE")
    os.makedirs(a.outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    shots = []

    from playwright.sync_api import sync_playwright
    chrome = (os.environ.get("CHROME_PATH") or os.path.expanduser(
        "~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"))
    ck = []
    if cookie:
        parts = dict(p.split("=", 1) for p in cookie.split(";") if "=" in p)
        ck = [{"name": k.strip(), "value": v.strip(), "domain": ".naver.com",
               "path": "/"} for k, v in parts.items()]
    mob = f"https://m.cafe.naver.com/ca-fe/web/cafes/{a.club_id}/articles/{aid}"

    with sync_playwright() as pw:
        kw = {"headless": True,
              "args": ["--no-sandbox", "--disable-gpu",
                       "--disable-dev-shm-usage"]}
        if os.path.exists(chrome):
            kw["executable_path"] = chrome
        b = pw.chromium.launch(**kw)
        ctx = b.new_context(user_agent=(
            "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"))
        if ck:
            ctx.add_cookies(ck)
        page = ctx.new_page()
        page.goto(mob, timeout=45000)
        page.wait_for_timeout(7000)
        if "nidlogin" in page.url:
            die("open", "login wall — cookie expired or invalid", shots, page.url)
        for _ in range(4):
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(1200)

        def find_scope():
            """Return the first matching comment-list selector and locators."""
            for sel in LIST_SELECTORS:
                for scope in [page] + [f for f in page.frames
                                       if f != page.main_frame]:
                    try:
                        els = scope.locator(sel)
                        if els.count():
                            return sel, els, scope
                    except Exception:
                        continue
            return None, None, None

        def read_count():
            try:
                t = page.evaluate(COUNT_JS)
                m = re.search(r"(\d+)", t or "")
                return int(m.group(1)) if m else None
            except Exception:
                return None

        sel, els, scope = find_scope()
        comments = []
        if sel and els:
            raw = els.evaluate_all(COMMENT_JS)
            for i, c in enumerate(raw):
                comments.append({
                    "index": i,
                    "author": c.get("author") or "",
                    "text": (c.get("text") or c.get("all") or "").strip(),
                    "time": c.get("time") or "",
                    "isReply": bool(c.get("isReply")),
                })
        declared = read_count()

        if a.list:
            msg = ""
            if not comments and declared is None:
                msg = "comment module not detected — skin/structure may have changed"
            elif not comments and declared == 0:
                msg = "no comments"
            elif declared and len(comments) < declared:
                msg = f"{len(comments)} of {declared} shown — more pagination needed"
            out({"status": "ok", "count": len(comments),
                 "declared_count": declared, "comments": comments,
                 "selector": sel or "", "url": page.url, "message": msg})
            b.close()
            return

        # write the comment
        opened = False
        if a.reply_index >= 0 and els is not None:
            try:
                item = els.nth(a.reply_index)
                item.scroll_into_view_if_needed()
                btn = item.locator("button.btn_write").first
                if btn.count():
                    btn.click()
                    opened = True
            except Exception:
                pass
        if not opened:
            for s2 in WRITE_BUTTON_SELECTORS:
                try:
                    el = page.locator(s2).first
                    if el.count() and el.is_visible():
                        el.click()
                        opened = True
                        break
                except Exception:
                    continue
        if not opened:
            s = os.path.join(a.outdir, f"fail_comment_writebtn_{stamp}.png")
            page.screenshot(path=s)
            die("open-editor", "write-comment button not found", [s], page.url)
        page.wait_for_timeout(2500)

        input_el = None
        deadline = time.time() + 15
        while time.time() < deadline and input_el is None:
            for scope2 in [page] + [f for f in page.frames
                                    if f != page.main_frame]:
                for s3 in INPUT_SELECTORS:
                    try:
                        el = scope2.locator(s3).first
                        if el.count() and el.is_visible():
                            input_el = el
                            break
                    except Exception:
                        continue
                if input_el:
                    break
            if not input_el:
                page.wait_for_timeout(1000)
        if not input_el:
            s = os.path.join(a.outdir, f"fail_comment_input_{stamp}.png")
            page.screenshot(path=s)
            die("input", "comment editor input not found", [s], page.url)
        input_el.scroll_into_view_if_needed()
        input_el.click()
        page.keyboard.insert_text(a.message)
        page.wait_for_timeout(800)
        shot = os.path.join(a.outdir, f"comment_draft_{stamp}.png")
        page.screenshot(path=shot)
        shots.append(shot)

        if not a.submit:
            out({"status": "draft", "count": len(comments),
                 "comments": comments, "screenshots": shots,
                 "message": "comment filled — review, then submit with --submit"})
            b.close()
            return

        clicked = False
        for s4 in SUBMIT_SELECTORS:
            try:
                el = page.locator(s4).first
                if el.count() and el.is_visible():
                    el.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            die("submit",
                "register button not found — check the draft screenshot",
                shots, page.url)
        page.wait_for_timeout(4000)
        shot2 = os.path.join(a.outdir, f"comment_submitted_{stamp}.png")
        page.screenshot(path=shot2)
        shots.append(shot2)
        out({"status": "submitted-unverified", "count": len(comments),
             "comments": comments, "screenshots": shots,
             "message": "register clicked — verify the final result from the screenshots"})
        b.close()


if __name__ == "__main__":
    main()
