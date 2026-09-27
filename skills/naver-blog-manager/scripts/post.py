#!/usr/bin/env python3
"""Naver blog post writer — draft-first (Playwright backend).

Usage:
  python3 post.py \
      --blog exampleblog --title "TITLE" --content-file body.md \
      [--images a.jpg,b.png] [--category NAME] \
      [--visibility private|public] [--submit] [--editor-url URL]

Auth    : NAVER_COOKIE env — server environment only (never printed/stored).
Default : opens the editor, fills title/body/images and saves a screenshot only
          (no publish). With --submit it publishes from the publish layer using
          --visibility (default private). Publishing cannot be undone — always
          review the screenshot and keep frequency low.
Output  : one JSON line {status, step, editor_url, screenshots[], message}
Note    : first live verification still required — if the SE ONE editor structure
          changes, enter via --editor-url or adjust the selectors below.
"""
import argparse
import json
import os
import sys
import time

TITLE_SELECTORS = [
    ".se-title-text",
    # Naver UI text (functional — matched against live pages): title placeholder.
    "input[placeholder*='제목']",
    ".se-input-title",
    ".se_title",
]
CONTENT_SELECTORS = [
    ".se-component-content",
    ".se-content [contenteditable='true']",
    "div[contenteditable='true']",
]
IMAGE_BUTTONS = [
    "[data-name='image']",
    # Naver UI text (functional — matched against live pages): photo button label.
    "button[aria-label*='사진']",
    "button[class*='image']",
]
# Naver UI text (functional — matched against live pages): publish button label.
SUBMIT_TEXTS = "발행"


def out(p):
    print(json.dumps(p, ensure_ascii=False))


def die(step, msg, shots=(), url=""):
    out({"status": "error", "step": step, "message": msg,
         "editor_url": url, "screenshots": list(shots)})
    sys.exit(1)


def find_ctx_and_el(root, selectors, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        contexts = [root] + [f for f in root.frames if f != root.main_frame]
        for ctx in contexts:
            for sel in selectors:
                try:
                    el = ctx.locator(sel).first
                    if el.count() and el.is_visible():
                        return ctx, sel, el
                except Exception:
                    continue
        root.wait_for_timeout(1000)
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blog", required=True, help="blog ID")
    ap.add_argument("--title", required=True)
    ap.add_argument("--content-file", required=True)
    ap.add_argument("--images", default="", help="comma-separated image paths")
    ap.add_argument("--category", default="", help="category name (optional)")
    ap.add_argument("--visibility", choices=["private", "public"],
                    default="private", help="visibility when publishing (default private)")
    ap.add_argument("--submit", action="store_true",
                    help="also publish (irreversible — review the draft first)")
    ap.add_argument("--outdir", default=os.path.expanduser(
        "~/.hermes/data/naver-blog/manage"))
    ap.add_argument("--editor-url", default="")
    ap.add_argument("--wait-editor", type=int, default=30)
    a = ap.parse_args()

    cookie = os.environ.get("NAVER_COOKIE")
    if not cookie:
        die("init", "NAVER_COOKIE not set — export it (see README §4)")
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

        # 1) enter the editor — direct URL first, else click the write button on the blog home
        editor = None
        if a.editor_url:
            page.goto(a.editor_url, timeout=45000)
            editor = page
        else:
            page.goto(f"https://blog.naver.com/{a.blog}/postwrite",
                      timeout=45000)
            page.wait_for_timeout(5000)
            tctx, _, probe = find_ctx_and_el(page, TITLE_SELECTORS, 6)
            if "nidlogin" in page.url:
                s = os.path.join(a.outdir, f"fail_login_{stamp}.png")
                page.screenshot(path=s)
                die("open-editor", "login wall — cookie expired/invalid", [s], page.url)
            if probe is None:
                # home → "write" fallback
                page.goto(f"https://blog.naver.com/{a.blog}", timeout=45000)
                page.wait_for_timeout(4000)
                clicked = False
                try:
                    with ctx.expect_page(timeout=8000) as pop:
                        # Naver UI text (functional — matched against live pages): write-button label.
                        page.get_by_text("글쓰기", exact=True).first.click()
                    editor = pop.value
                    clicked = True
                except Exception:
                    pass
                if not clicked:
                    try:
                        # Naver UI text (functional — matched against live pages): write-button label.
                        page.get_by_text("글쓰기", exact=True).first.click()
                        page.wait_for_timeout(4000)
                        editor = page
                        clicked = True
                    except Exception as e:
                        s = os.path.join(a.outdir, f"fail_editor_{stamp}.png")
                        page.screenshot(path=s)
                        die("open-editor", f"failed to enter the editor: {e}", [s], page.url)
            else:
                editor = page
        try:
            editor.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass

        # 2) title
        _, _, title_el = find_ctx_and_el(editor, TITLE_SELECTORS,
                                         a.wait_editor)
        if not title_el:
            s = os.path.join(a.outdir, f"fail_title_{stamp}.png")
            try:
                editor.screenshot(path=s)
                shots.append(s)
            except Exception:
                pass
            die("fill-title",
                f"title input not found (url={editor.url})", shots, editor.url)
        title_el.click()
        editor.keyboard.insert_text(a.title)
        editor.wait_for_timeout(700)

        # 3) body
        _, _, content_el = find_ctx_and_el(editor, CONTENT_SELECTORS, 20)
        if not content_el:
            s = os.path.join(a.outdir, f"fail_content_{stamp}.png")
            try:
                editor.screenshot(path=s)
                shots.append(s)
            except Exception:
                pass
            die("fill-content", "body editor area not found", shots, editor.url)
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
            # toolbar photo button → file chooser
            for sel in IMAGE_BUTTONS:
                try:
                    btn = editor.locator(sel).first
                    if btn.count() and btn.is_visible():
                        with editor.expect_file_chooser(timeout=8000) as fc:
                            btn.click()
                        fc.value.set_files(images)
                        attached = True
                        break
                except Exception:
                    continue
            if not attached:
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
                    "image attach failed — no file chooser found",
                    shots, editor.url)
            editor.wait_for_timeout(5000)

        # 5) draft screenshot
        shot = os.path.join(a.outdir, f"draft_{stamp}.png")
        try:
            editor.screenshot(path=shot, full_page=True)
        except Exception:
            editor.screenshot(path=shot)
        shots.append(shot)

        # 6) publish (optional)
        if not a.submit:
            out({"status": "draft", "step": "filled",
                 "editor_url": editor.url, "screenshots": shots,
                 "message": "draft filled — review, then publish with --submit"})
            b.close()
            return

        clicked = False
        for sel in [f"button:has-text('{SUBMIT_TEXTS}')",
                    "[class*='publish_btn']", "[class*='btn_publish']"]:
            try:
                el = editor.locator(sel).first
                if el.count() and el.is_visible():
                    el.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            die("publish", "publish button not found — check the draft screenshot",
                shots, editor.url)
        editor.wait_for_timeout(2500)
        # publish layer: visibility setting
        layer = None
        for sel in [".layer_publish", "[class*='layer_publish']",
                    "[class*='publish_layer']"]:
            try:
                el = editor.locator(sel).first
                if el.count() and el.is_visible():
                    layer = el
                    break
            except Exception:
                continue
        if layer is not None:
            try:
                # Naver UI text (functional — matched against live pages): visibility radio labels.
                vis = ("label:has-text('비공개')" if a.visibility == "private"
                       else "label:has-text('공개')")
                el = layer.locator(vis).first
                if el.count():
                    el.click()
                    editor.wait_for_timeout(500)
            except Exception:
                pass
            if a.category:
                try:
                    sel = layer.locator("select").first
                    if sel.count():
                        sel.select_option(label=a.category)
                except Exception:
                    pass
            try:
                el = layer.locator(f"button:has-text('{SUBMIT_TEXTS}')").last
                if el.count():
                    el.click()
            except Exception:
                pass
        editor.wait_for_timeout(5000)
        shot2 = os.path.join(a.outdir, f"submitted_{stamp}.png")
        try:
            editor.screenshot(path=shot2)
            shots.append(shot2)
        except Exception:
            pass
        out({"status": "submitted-unverified", "step": "publish",
             "editor_url": editor.url, "screenshots": shots,
             "message": "publish flow executed — verify with the screenshot"})
        b.close()


if __name__ == "__main__":
    main()
