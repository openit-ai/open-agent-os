#!/usr/bin/env python3
"""First-run setup: pick cafes/boards/keywords -> config.json.

Run: python3 setup_watch.py [--config PATH]
Board list needs NO login. Writes:
  ~/.hermes/data/naver-cafe-watch/config.json
"""
import argparse
import json
import os
import re
import sys

try:
    import requests
except ImportError:
    sys.exit("requests missing")

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
DEFAULT_CONFIG = os.path.expanduser(
    "~/.hermes/data/naver-cafe-watch/config.json")


def fetch_boards(domain):
    r = requests.get(f"https://cafe.naver.com/{domain}",
                     headers=UA, timeout=20)
    t = r.content.decode("euc-kr", errors="ignore")
    clubs = set(re.findall(r"clubid=(\d+)", t))
    if not clubs:
        sys.exit("cafe not found — check the domain.")
    seen = {}
    for mid, name in re.findall(
            r"menuid=(\d+)[^<>]{0,200}?>([^<>]{1,30})<", t):
        name = name.strip()
        if name and mid not in seen:
            seen[mid] = name
    return clubs.pop(), seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    a = ap.parse_args()
    cfg = {"keywords": ["21700", "18650"], "cafes": []}
    if os.path.exists(a.config):
        cfg = json.load(open(a.config))
    print("Cafe domain (e.g. <cafe-domain>):")
    domain = input().strip()
    club_id, boards = fetch_boards(domain)
    print(f"club_id={club_id}, {len(boards)} boards")
    for mid in sorted(boards, key=int):
        print(f"  {mid}: {boards[mid]}")
    print("Boards to watch — comma-separated menu ids (e.g. <board-id>):")
    picked = [m.strip() for m in input().split(",") if m.strip()]
    bad = [m for m in picked if m not in boards]
    if bad:
        sys.exit(f"unknown menu id(s): {bad}")
    print("Keywords, comma-separated (Enter = 21700,18650):")
    kws = [k.strip() for k in input().split(",") if k.strip()]
    if kws:
        cfg["keywords"] = kws
    cfg["cafes"] = [c for c in cfg["cafes"] if c["cafe"] != domain]
    cfg["cafes"].append({
        "cafe": domain, "club_id": club_id,
        "boards": [{"menu_id": m, "label": boards[m]} for m in picked]})
    os.makedirs(os.path.dirname(a.config), exist_ok=True)
    json.dump(cfg, open(a.config, "w"), ensure_ascii=False, indent=1)
    print(f"saved: {a.config}")
    print("watch targets:",
          [(c["cafe"], [b["menu_id"] for b in c["boards"]])
           for c in cfg["cafes"]])


if __name__ == "__main__":
    main()
