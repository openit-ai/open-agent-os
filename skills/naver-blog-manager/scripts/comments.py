#!/usr/bin/env python3
"""Naver blog comments — list and reply (draft-first, Playwright, mobile CBOX).

Structure measured (2026-09-27, public post with 10 comments — count matched):
- Mobile post page → click the comments button → CBOX loads:
  items li.u_cbox_comment / .u_cbox_nick / .u_cbox_contents / .u_cbox_date;
  secret comments render a masked label, the blog-owner badge is marked on the item.
- Write: .u_cbox_write (contenteditable .u_cbox_text) → register button.
- Listing works without login; writing needs a logged-in session (NAVER_COOKIE).

Usage:
  # List (read-only — listing is the default when no --message is given)
  python3 comments.py --blog <blog-id> --log-no <log-no> --list

  # Comment draft (not registered — requires --submit to register)
  python3 comments.py --blog <blog-id> --log-no <log-no> \
      --message "Hello, I enjoyed the post."

  # Reply to a specific comment (index = index from --list output)
  python3 comments.py --blog <blog-id> --log-no <log-no> \
      --message "Thank you!" --reply-index 0 --submit
"""
import argparse
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

COOKIE_ENV = "NAVER_COOKIE"
UA = ("Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")
# Browser binary — override with CHROME_PATH when Playwright's cache layout differs.
CHROME = os.environ.get("CHROME_PATH") or os.path.expanduser(
    "~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome")

EXTRACT_RATE_LIMIT = 60  # seconds — not an API call; documentation-only note


def out(obj):
    print(json.dumps(obj, ensure_ascii=False))


def die(msg, code=1):
    out({"status": "error", "message": msg})
    sys.exit(code)


EXTRACT_JS = """() => {
  const items = [...document.querySelectorAll('.u_cbox_comment')];
  const comments = items.map((e, i) => {
    const info = e.querySelector('.u_cbox_info');
    const ownNick = info ? info.querySelector('.u_cbox_nick') : null;
    const g = (s) => { const n = e.querySelector(s); return n ? n.innerText.trim() : ''; };
    const secret = !!info && !ownNick;
    const ownMeta = info ? info.innerText : '';
    // Naver UI text (functional — matched against live pages):
    // '(비밀)' / '비밀 댓글입니다.' mirror the page's own secret-comment labels,
    // and /블로그주인/ matches the blog-owner badge text.
    return {
      index: i,
      author: secret ? '(비밀)' : (ownNick ? ownNick.innerText.trim() : ''),
      text: secret ? '비밀 댓글입니다.' : g('.u_cbox_contents'),
      time: g('.u_cbox_date'),
      secret,
      is_owner: /블로그주인/.test(ownMeta),
      is_reply: !!e.closest('.u_cbox_reply_area'),
      can_reply: !!e.querySelector('.u_cbox_btn_reply'),
    };
  });
  // Naver UI text (functional — matched against live pages): declared comment count.
  const m = (document.body.innerText || '').match(/댓글\\s*\\n?\\s*(\\d+)/);
  return {comments, declared: m ? parseInt(m[1], 10) : null,
          hasWrite: !!document.querySelector('.u_cbox_write')};
}"""


def visible_first(page, selector):
    loc = page.locator(selector)
    for i in range(loc.count()):
        el = loc.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:
            continue
    return None


def open_comment_panel(page, timeout_s=14):
    """Click the comments button and wait for CBOX to render."""
    # Naver UI text (functional — matched against live pages): comments button label.
    btn = visible_first(page, "button:has-text('댓글'), a:has-text('댓글')")
    if btn:
        try:
            btn.scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            btn.click(timeout=8000)
        except Exception:
            pass
    deadline = timeout_s * 1000
    step = 500
    waited = 0
    while waited < deadline:
        try:
            if page.locator(".u_cbox").count():
                return True
        except Exception:
            pass
        page.wait_for_timeout(step)
        waited += step
    return page.locator(".u_cbox").count() > 0


def shot(page, out_dir, name):
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, name)
        page.screenshot(path=path, full_page=False)
        return path
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blog", required=True)
    ap.add_argument("--log-no", required=True)
    ap.add_argument("--list", action="store_true", help="list comments (default)")
    ap.add_argument("--message", help="comment text to type")
    ap.add_argument("--reply-index", type=int, default=-1,
                    help="reply target comment index (from --list output)")
    ap.add_argument("--submit", action="store_true",
                    help="actually register (without it: draft only)")
    ap.add_argument("--out-dir", default=os.path.expanduser("~/data/naver-blog"))
    a = ap.parse_args()
    if not a.message:
        a.list = True

    cookie = os.environ.get(COOKIE_ENV, "")
    parts = dict(p.split("=", 1) for p in cookie.split(";") if "=" in p) \
        if cookie else {}
    ck = [{"name": k.strip(), "value": v.strip(),
           "domain": ".naver.com", "path": "/"} for k, v in parts.items()]
    if not ck:
        out({"status": "warn", "message": f"{COOKIE_ENV} not set — listing "
             "works without login, writing may fail"})

    url = f"https://m.blog.naver.com/{a.blog}/{a.log_no}"
    with sync_playwright() as pw:
        kw = dict(headless=True, args=["--no-sandbox", "--disable-gpu",
                                       "--disable-dev-shm-usage"])
        if os.path.exists(CHROME):
            kw["executable_path"] = CHROME
        b = pw.chromium.launch(**kw)
        ctx = b.new_context(user_agent=UA)
        if ck:
            ctx.add_cookies(ck)
        page = ctx.new_page()
        try:
            page.goto(url, timeout=45000)
            page.wait_for_timeout(4000)
            for _ in range(4):
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(700)
            found = open_comment_panel(page)
            if not found:
                out({"status": "ok", "count": 0, "comments": [],
                     "declared_count": 0, "url": url,
                     "message": "comment module not detected (no comments / possibly collapsed)"})
                return
            res = page.evaluate(EXTRACT_JS)
            comments = res.get("comments", [])
            declared = res.get("declared")

            if a.list:
                msg = ""
                if not comments and declared == 0:
                    msg = "0 comments"
                elif declared and len(comments) < declared:
                    msg = (f"showing {len(comments)} of {declared} "
                           "— more may need expanding")
                out({"status": "ok", "count": len(comments),
                     "declared_count": declared, "comments": comments,
                     "url": page.url, "message": msg})
                return

            # --- write (draft-first) ---
            if a.reply_index >= 0:
                items = page.locator(".u_cbox_comment")
                if a.reply_index >= items.count():
                    die(f"reply-index {a.reply_index} out of range "
                        f"({items.count()} comments)")
                item = items.nth(a.reply_index)
                # Naver UI text (functional — matched against live pages): reply button label.
                rb = item.locator(
                    ".u_cbox_btn_reply, a:has-text('답글'), "
                    "button:has-text('답글')")
                if not rb.count() or not rb.first.is_visible():
                    die(f"no reply button on comment #{a.reply_index} "
                        "(secret comment, etc.)")
                item.scroll_into_view_if_needed()
                rb.first.click()
                page.wait_for_timeout(1500)
                scope = item
            else:
                scope = page

            ta = scope.locator(
                ".u_cbox_write .u_cbox_text, .u_cbox_text, "
                "[contenteditable='true']").last
            try:
                ta.wait_for(state="visible", timeout=8000)
                ta.scroll_into_view_if_needed()
                ta.click()
                ta.fill(a.message)
            except Exception as e:
                p = shot(page, a.out_dir, "blog-comment-error.png")
                die(f"could not reach the input box: {str(e)[:140]} (screenshot: {p})")
            page.wait_for_timeout(500)
            if not a.submit:
                p = shot(page, a.out_dir, "blog-comment-draft.png")
                out({"status": "draft", "message": "draft typed (not registered)",
                     "reply_index": a.reply_index, "screenshot": p,
                     "url": page.url})
                return
            # Naver UI text (functional — matched against live pages): register button label.
            submit = scope.locator(
                ".u_cbox_btn_upload:not(.u_cbox_btn_upload_sticker), "
                "a:has-text('등록'), button:has-text('등록')").last
            if not submit.count():
                p = shot(page, a.out_dir, "blog-comment-nosubmit.png")
                die(f"register button not found (screenshot: {p})")
            submit.click()
            page.wait_for_timeout(4000)
            p = shot(page, a.out_dir, "blog-comment-submitted.png")
            out({"status": "submitted", "message": "register clicked — "
                 "re-list to confirm", "screenshot": p, "url": page.url})
        finally:
            b.close()


if __name__ == "__main__":
    main()
