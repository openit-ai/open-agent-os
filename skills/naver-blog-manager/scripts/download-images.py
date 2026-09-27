#!/usr/bin/env python3
"""Collected blog md → bulk download of original-resolution images (generic).
Usage: python3 download-images.py <collected-md-path>
Output: <md-folder>/images/ with the images + manifest.csv
Strategy: mblogthumb collected URL → postfiles.pstatic.net?type=w3840 → blogfiles.pstatic.net (no query) → original URL?type=w966 fallback.
"""
import csv
import re
import struct
import subprocess
import sys
import pathlib
import time
from collections import Counter

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

def curl(url, out):
    r = subprocess.run(
        ["curl", "-s", "-m", "60", "-A", UA, "-o", out, "-w", "%{http_code}", url],
        capture_output=True, text=True)
    return r.stdout.strip()

def img_dims(path):
    """JPEG/PNG/GIF width,height"""
    data = pathlib.Path(path).read_bytes()
    if data[:2] == b"\xff\xd8":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return (w, h, '.jpg')
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        w, h = struct.unpack(">II", data[16:24])
        return (w, h, '.png')
    if data[:3] == b'GIF':
        w, h = struct.unpack("<HH", data[6:10])
        return (w, h, '.gif')
    return None

def main():
    if len(sys.argv) < 2:
        print("usage: python3 download-images.py <collected-md-path>"); sys.exit(1)
    MD = pathlib.Path(sys.argv[1]).resolve()
    OUTDIR = MD.parent / 'images'
    OUTDIR.mkdir(parents=True, exist_ok=True)

    text = MD.read_text()
    posts = re.split(r'\n## ', text)[1:]
    jobs = []
    for pno, p in enumerate(posts, 1):
        for idx, u in enumerate(re.findall(r'!\[\]\(([^)]+)\)', p), 1):
            u = u.strip().strip('\r\n\t ')
            if u:
                jobs.append((pno, idx, u))
    print(f"fetching {len(jobs)} images → {OUTDIR}")

    rows, ok, fail, total_bytes = [], 0, 0, 0
    consecutive_fail = 0
    log = open(OUTDIR / '.download.log', 'w')
    for n, (pno, idx, url) in enumerate(jobs, 1):
        path = url.split('//', 1)[1].split('/', 1)[1]
        candidates = [
            ('postfiles-w3840', f"https://postfiles.pstatic.net/{path}?type=w3840"),
            ('blogfiles-orig', f"https://blogfiles.pstatic.net/{path}"),
            ('source-w966', f"{url}?type=w966"),
        ]
        tmp = OUTDIR / f".tmp-{pno:02d}-{idx:03d}"
        saved = False
        codes = []
        for src, cand in candidates:
            code = curl(cand, str(tmp))
            codes.append(f"{src}:{code}")
            if code == "200" and tmp.exists() and tmp.stat().st_size > 0:
                dims = img_dims(tmp)
                if dims:
                    f = OUTDIR / f"post{pno:02d}-{idx:03d}{dims[2]}"
                    tmp.rename(f)
                    size = f.stat().st_size
                    rows.append((f.name, pno, idx, src, dims[0], dims[1], size, url))
                    ok += 1
                    total_bytes += size
                    saved = True
                    break
            tmp.unlink(missing_ok=True)
        log.write(f"[{n}] post{pno:02d}-{idx:03d} {' '.join(codes)} saved={saved}\n")
        if not saved:
            fail += 1
            rows.append((f"post{pno:02d}-{idx:03d}(FAIL)", pno, idx, 'ALL-FAIL', '', '', 0, url))
            consecutive_fail += 1
            if consecutive_fail >= 3:
                log.write("  cooldown 30s...\n"); log.flush()
                time.sleep(30)
                consecutive_fail = 0
        else:
            consecutive_fail = 0
        if n % 10 == 0:
            print(f"  progress {n}/{len(jobs)} (ok {ok} failed {fail})"); log.flush()
        time.sleep(0.6)
    log.close()

    with open(OUTDIR / 'manifest.csv', 'w', newline='') as fp:
        w = csv.writer(fp)
        w.writerow(['file', 'post', 'idx', 'source', 'width', 'height', 'bytes', 'md_url'])
        w.writerows(rows)
    print(f"ok {ok} / failed {fail} / total {total_bytes/1024/1024:.1f} MB")
    withs = [r[4] for r in rows if r[4] != '']
    if withs:
        ws = sorted(withs)
        print(f"width min {ws[0]} / median {ws[len(ws)//2]} / max {ws[-1]}")
    print("source distribution:", dict(Counter(r[3] for r in rows)))

if __name__ == '__main__':
    main()
