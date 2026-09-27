#!/usr/bin/env python3
"""Naver blog new-post watcher — read-only, cron-friendly.

Usage:
  python3 watch.py [--blog ID]... [--count N] [--config FILE] [--state FILE] [--json]

Config (created as an empty template if missing): ~/.hermes/data/naver-blog-manager/watch.json
  {"blogs": ["example1", "example2"], "count": 10}
State : ~/.hermes/data/naver-blog-manager/state.json
Output: new posts only on stdout (JSON with --json). Silent exit (empty stdout) when
        nothing is new. The first run for a blog stores a baseline only — no alerts
        (prevents a flood of historical posts).
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
      "Mobile/15E148 Safari/604.1")
DATA_DIR = os.path.expanduser("~/.hermes/data/naver-blog-manager")
DEF_CONFIG = os.path.join(DATA_DIR, "watch.json")
DEF_STATE = os.path.join(DATA_DIR, "state.json")


def fetch_list(blog, count):
    url = (f"https://blog.naver.com/PostTitleListAsync.naver?blogId={blog}"
           f"&countPerPage={count}&currentPage=1")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": f"https://blog.naver.com/{blog}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        text = r.read().decode("utf-8", "replace")
    posts = []
    try:
        data = json.loads(text)
        for p in data.get("postList", []):
            posts.append({
                "logNo": str(p.get("logNo")),
                "title": urllib.parse.unquote_plus(str(p.get("title", ""))),
                "date": str(p.get("addDate") or ""),
            })
    except Exception:
        pat = (r"logNo[\"']?\s*:\s*[\"'](\d+)[\"'][\s\S]{0,600}?"
               r"title[\"']?\s*:\s*[\"']([^\"']*)[\"'][\s\S]{0,300}?"
               r"addDate[\"']?\s*:\s*[\"']([^\"']*)[\"']")
        for m in re.finditer(pat, text):
            posts.append({
                "logNo": m.group(1),
                "title": urllib.parse.unquote_plus(m.group(2)),
                "date": m.group(3),
            })
    return posts[:count]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blog", action="append", default=[],
                    help="blog ID to watch (repeatable)")
    ap.add_argument("--count", type=int, default=0,
                    help="recent posts to check per blog (default 10)")
    ap.add_argument("--config", default=DEF_CONFIG)
    ap.add_argument("--state", default=DEF_STATE)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    blogs, count = list(a.blog), a.count
    if os.path.exists(a.config):
        try:
            cfg = json.load(open(a.config))
            if not blogs:
                blogs = cfg.get("blogs", [])
            if not count:
                count = int(cfg.get("count", 10))
        except Exception as e:
            print(f"failed to read config: {e}", file=sys.stderr)
    elif not blogs:
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(a.config):
            json.dump({"blogs": [], "count": 10},
                      open(a.config, "w"), ensure_ascii=False, indent=1)
            print(f"created empty watch config: {a.config}", file=sys.stderr)
    if not count:
        count = 10
    if not blogs:
        print("No blogs to watch. Fill 'blogs' in the watch config or pass "
              "--blog <id>.", file=sys.stderr)
        sys.exit(2)

    state = {}
    if os.path.exists(a.state):
        try:
            state = json.load(open(a.state))
        except Exception:
            state = {}

    new_all, errors = [], []
    for blog in blogs:
        try:
            posts = fetch_list(blog, count)
        except Exception as e:
            errors.append((blog, str(e)))
            continue
        known = state.get(blog)
        if known is None:
            state[blog] = [p["logNo"] for p in posts]
            print(f"[{blog}] baseline saved ({len(posts)} posts) — no alerts",
                  file=sys.stderr)
            continue
        known_set = set(known)
        for p in posts:
            if p["logNo"] not in known_set:
                new_all.append({"blog": blog, "logNo": p["logNo"],
                                "title": p["title"], "date": p["date"],
                                "url": f"https://blog.naver.com/{blog}/{p['logNo']}"})
        merged = [p["logNo"] for p in posts] + known
        state[blog] = list(dict.fromkeys(merged))[:300]

    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(state, open(a.state, "w"), ensure_ascii=False, indent=1)

    if errors:
        for blog, e in errors:
            print(f"[{blog}] fetch failed: {e}", file=sys.stderr)
    if a.json:
        print(json.dumps({"new": new_all, "checked": len(blogs),
                          "errors": [{"blog": b, "error": e}
                                     for b, e in errors]},
                         ensure_ascii=False))
    else:
        for p in new_all:
            print(f"[{p['blog']}] {p['date']} | {p['title']} | {p['url']}")
    print(f"checked {len(blogs)} blogs · {len(new_all)} new"
          + (f" · {len(errors)} errors" if errors else ""), file=sys.stderr)


if __name__ == "__main__":
    main()
