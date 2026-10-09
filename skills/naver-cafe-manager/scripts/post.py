#!/usr/bin/env python3
"""Naver cafe post writer — draft-first (Playwright backend).

Usage:
  python3 post.py \
      --cafe <cafe-domain> --club-id <club-id> --menu-id <board-id> \
      --title "post title" --content-file body.md \
      [--images a.jpg,b.png] [--submit] [--editor-url URL]

Auth    : NAVER_COOKIE env — server .env only (register_cookie.py).
Default : opens the editor, fills title/content/images, saves a screenshot
          and stops (nothing is submitted). Only --submit presses the
          register button. Submission is irreversible — always review the
          screenshot and use it at a low frequency (macro-detection risk).
Output  : one-line JSON {status, step, editor_url, screenshots[], message}
"""
import argparse
import json
import os
import sys
import time

# Naver UI text (functional — matched against live pages)
TITLE_SELECTORS = [
    "input.se-title-text",
    "input[placeholder*='제목']",
    "textarea[placeholder*='제목']",
    ".article_title input",
    "input.input_title",
]
CONTENT_SELECTORS = [
    "div.se-component-content",
    "div.se-content [contenteditable='true']",
    "div[contenteditable='true']",
]
# Naver UI text (functional — matched against live pages)
SUBMIT_NAME = "등록|글등록|발행"


def out(payload):
    print(json.dumps(payload, ensure_ascii=False))


def die(step, msg, shots=(), url=""):
    out({"status": "error", "step": step, "message": msg,
         "editor_url": url, "screenshots": list(shots)})
    sys.exit(1)


def find_ctx_and_el(editor, selectors, timeout_s):
    """Find the first visible element across the editor page and its frames."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        contexts = [editor] + [f for f in editor.frames
                               if f != editor.main_frame]
        for ctx in contexts:
            for sel in selectors:
                try:
                    el = ctx.locator(sel).first
                    if el.count() and el.is_visible():
                        return ctx, sel, el
                except Exception:
                    continue
        editor.wait_for_timeout(1000)
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cafe", required=True,
                    help="cafe domain (e.g. <cafe-domain>)")
    ap.add_argument("--club-id", required=True)
    ap.add_argument("--menu-id", required=True,
                    help="board menuid to post into")
    ap.add_argument("--title", required=True)
    ap.add_argument("--content-file", required=True,
                    help="body file (.md/.txt)")
    ap.add_argument("--images", default="",
                    help="comma-separated image paths")
    ap.add_argument("--submit", action="store_true",
                    help="also submit (irreversible — review the draft first)")
    ap.add_argument("--outdir", default=os.path.expanduser(
        "~/.hermes/data/naver-cafe/manage"))
    ap.add_argument("--editor-url", default="",
                    help="set the editor URL directly (when board entry fails)")
    ap.add_argument("--wait-editor", type=int, default=30)
    a = ap.parse_args()

    cookie = os.environ.get("NAVER_COOKIE")
    if not cookie:
        die("init", "NAVER_COOKIE missing — register it via register_cookie.py")
    if not os.path.exists(a.content_file):
        die("init", f"content file not found: {a.content_file}")
    content = open(a.content_file, encoding="utf-8").read()[:8000]
    images = [os.path.expanduser(p.strip())
              for p in a.images.split(",") if p.strip()]
    for p in images:
        if not os.path.exists(p):
            die("init", f"image file not found: {p}")
    os.makedirs(a.outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    shots = []

    from playwright.sync_api import sync_playwright
    chrome = (os.environ.get("CHROME_PATH") or os.path.expanduser(
        "~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"))
    parts = dict(p.split("=", 1) for p in cookie.split(";") if "=" in p)
    ck = [{"name": k.strip(), "value": v.strip(), "domain": ".naver.com",
           "path": "/"} for k, v in parts.items()]

    with sync_playwright() as pw:
        kw = {"headless": True,
              "args": ["--no-sandbox", "--disable-gpu",
                       "--disable-dev-shm-usage"]}
        if os.path.exists(chrome):
            kw["executable_path"] = chrome
        b = pw.chromium.launch(**kw)
        ctx = b.new_context(user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"))
        ctx.add_cookies(ck)
        page = ctx.new_page()

        # 1) open the editor
        editor = None
        if a.editor_url:
            page.goto(a.editor_url, timeout=45000)
            editor = page
        else:
            board = (f"https://cafe.naver.com/f-e/cafes/{a.club_id}/menus/"
                     f"{a.menu_id}?viewType=L")
            page.goto(board, timeout=45000)
            page.wait_for_timeout(4000)
            if "nidlogin" in page.url:
                s = os.path.join(a.outdir, f"fail_login_{stamp}.png")
                page.screenshot(path=s)
                die("open-board", "login wall — cookie expired or invalid",
                    [s], page.url)
            clicked = False
            # Naver UI text (functional — matched against live pages)
            try:
                with ctx.expect_page(timeout=8000) as pop:
                    page.get_by_text("글쓰기", exact=True).first.click()
                editor = pop.value
                clicked = True
            except Exception:
                pass
            if not clicked:
                # Naver UI text (functional — matched against live pages)
                try:
                    page.get_by_text("글쓰기", exact=True).first.click()
                    page.wait_for_timeout(3000)
                    editor = page
                    clicked = True
                except Exception as e:
                    s = os.path.join(a.outdir, f"fail_editor_{stamp}.png")
                    page.screenshot(path=s)
                    die("open-editor", f"could not open the write editor: {e}",
                        [s], page.url)
        try:
            editor.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass

        # 2) title
        tctx, _, title_el = find_ctx_and_el(editor, TITLE_SELECTORS,
                                            a.wait_editor)
        if not title_el:
            s = os.path.join(a.outdir, f"fail_title_{stamp}.png")
            try:
                editor.screenshot(path=s)
                shots.append(s)
            except Exception:
                pass
            die("fill-title",
                f"title field not found (editor structure may have changed, url={editor.url})",
                shots, editor.url)
        title_el.click()
        editor.keyboard.insert_text(a.title)
        editor.wait_for_timeout(700)

        # 3) body
        cctx, _, content_el = find_ctx_and_el(editor, CONTENT_SELECTORS, 20)
        if not content_el:
            s = os.path.join(a.outdir, f"fail_content_{stamp}.png")
            try:
                editor.screenshot(path=s)
                shots.append(s)
            except Exception:
                pass
            die("fill-content", "content editor area not found",
                shots, editor.url)
        content_el.click()
        editor.wait_for_timeout(500)
        for i, line in enumerate(content.split("\n")):
            if i:
                editor.keyboard.press("Enter")
            if line.strip():
                editor.keyboard.insert_text(line)
        editor.wait_for_timeout(800)

        # 4) attach images
        if images:
            attached = False
            for ctx2 in [editor] + [f for f in editor.frames
                                    if f != editor.main_frame]:
                try:
                    fi = ctx2.locator("input[type='file']").first
                    if fi.count():
                        fi.set_input_files(images)
                        attached = True
                        break
                except Exception:
                    continue
            if not attached:
                die("attach-images",
                    "file input not found — review without images and attach manually",
                    shots, editor.url)
            editor.wait_for_timeout(4000)

        # 5) screenshot for draft review
        shot = os.path.join(a.outdir, f"draft_{stamp}.png")
        try:
            editor.screenshot(path=shot, full_page=True)
        except Exception:
            editor.screenshot(path=shot)
        shots.append(shot)

        # 6) submit (optional)
        if not a.submit:
            out({"status": "draft", "step": "filled",
                 "editor_url": editor.url, "screenshots": shots,
                 "message": "draft filled — review, then submit with --submit"})
            b.close()
            return

        clicked = False
        # Naver UI text (functional — matched against live pages)
        for sel in [f"button:has-text('{SUBMIT_NAME}')",
                    f"a:has-text('{SUBMIT_NAME}')",
                    "[class*='btn_submit']", "[class*='submit']"]:
            try:
                el = editor.locator(sel).first
                if el.count() and el.is_visible():
                    el.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            die("submit",
                "register button not found — check the draft screenshot",
                shots, editor.url)
        editor.wait_for_timeout(2500)
        # confirm the submission modal when present
        # Naver UI text (functional — matched against live pages)
        for sel in [".btn_confirm", f"button:has-text('확인')",
                    f"button:has-text('등록')"]:
            try:
                el = editor.locator(sel).first
                if el.count() and el.is_visible():
                    el.click()
                    break
            except Exception:
                continue
        editor.wait_for_timeout(4000)
        shot2 = os.path.join(a.outdir, f"submitted_{stamp}.png")
        try:
            editor.screenshot(path=shot2)
            shots.append(shot2)
        except Exception:
            pass
        out({"status": "submitted-unverified", "step": "submit",
             "editor_url": editor.url, "screenshots": shots,
             "message": "register clicked — verify the final result from the screenshots"})
        b.close()


if __name__ == "__main__":
    main()
