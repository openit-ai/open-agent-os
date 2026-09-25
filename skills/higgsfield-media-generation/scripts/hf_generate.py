#!/usr/bin/env python3
"""Higgsfield API helper: estimate / image / video / r2v / status. Stdlib only.

Credentials from env: HF_API_KEY_ID, HF_API_KEY_SECRET (server .env only).
Docs: https://docs.higgsfield.ai/docs (verified 2026-09-18).
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.higgsfield.ai"
# Cloudflare 1010 workaround: a browser UA is required (the default Python UA is blocked before reaching the API)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
IMAGE_ENDPOINT = "higgsfield-ai/soul/v2/standard"
MS_IMAGE_ENDPOINT = "marketing-studio/image"
VIDEO_ENDPOINT = "bytedance/seedance-2.5/text-to-video"
I2V_ENDPOINT = "kling-video/v3.0-turbo/image-to-video"
R2V_ENDPOINT = "bytedance/seedance-2.5/reference-to-video"
TERMINAL = {"completed", "failed", "nsfw", "canceled"}
CTYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
          ".webp": "image/webp", ".gif": "image/gif",
          ".wav": "audio/wav", ".mp4": "video/mp4"}


def creds():
    kid = os.environ.get("HF_API_KEY_ID")
    sec = os.environ.get("HF_API_KEY_SECRET")
    if not kid or not sec:
        sys.exit("missing credentials: set HF_API_KEY_ID/HF_API_KEY_SECRET in server .env")
    return kid, sec


def call(method, path, body=None):
    kid, sec = creds()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Key " + kid + ":" + sec,
                  "Content-Type": "application/json", **UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:500]
        except Exception:
            detail = ""
        sys.exit("HTTP %s: %s" % (e.code, detail))


def estimate(endpoint, params):
    s, r = call("POST", "/estimate/" + endpoint, params)
    print(json.dumps(r, ensure_ascii=False))


def submit(endpoint, params):
    s, r = call("POST", "/" + endpoint, params)
    print(json.dumps(r, ensure_ascii=False))
    return r["request_id"]


def poll(request_id, timeout=900):
    delay, waited = 2.0, 0.0
    while True:
        s, r = call("GET", "/requests/" + request_id + "/status")
        if r["status"] in TERMINAL:
            print(json.dumps(r, ensure_ascii=False))
            return r
        if waited >= timeout:
            sys.exit("poll timeout after %ss, request_id=%s" % (timeout, request_id))
        time.sleep(delay)
        waited += delay
        delay = min(delay * 1.5, 10.0)


def upload_file(path):
    ext = os.path.splitext(path)[1].lower()
    ctype = CTYPES.get(ext)
    if not ctype:
        sys.exit(f"unsupported type: {ext}")
    s, r = call("POST", "/files/generate-upload-url", {"content_type": ctype})
    data = open(path, "rb").read()
    req = urllib.request.Request(
        r["upload_url"], data=data, method="PUT",
        headers=dict(r.get("upload_headers", {})))
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"upload HTTP {e.code}")
    print(r["public_url"])
    return r["public_url"]


def download(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    opener = urllib.request.build_opener()
    opener.addheaders = list(UA.items())
    with opener.open(url, timeout=120) as r, open(dest, "wb") as f:
        size = 0
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            size += len(chunk)
    print("saved: %s (%d bytes)" % (dest, size))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True,
                    choices=["estimate", "image", "video", "ms-image", "i2v", "r2v",
                             "upload", "presets", "status"])
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--file", default=None)
    ap.add_argument("--duration", type=int, default=5)
    ap.add_argument("--resolution", default="720p")
    ap.add_argument("--aspect-ratio", default="16:9")
    ap.add_argument("--quality", default=None)
    ap.add_argument("--preset-id", default=None)
    ap.add_argument("--image-url", action="append", default=None)
    ap.add_argument("--moderation", default="auto")
    ap.add_argument("--enhance-prompt", action="store_true")
    ap.add_argument("--request-id", default=None)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--estimate-only", action="store_true")
    ap.add_argument("--no-audio", action="store_true")
    a = ap.parse_args()

    if a.mode == "status":
        if not a.request_id:
            sys.exit("--request-id required")
        s, r = call("GET", "/requests/" + a.request_id + "/status")
        print(json.dumps(r, ensure_ascii=False))
        return

    if a.mode == "presets":
        s, r = call("GET", "/marketing-studio/image/presets?size=50")
        print(json.dumps(r, ensure_ascii=False)[:2000])
        return

    if a.mode == "upload":
        if not a.file:
            sys.exit("--file required")
        upload_file(a.file)
        return

    if not a.prompt:
        sys.exit("--prompt required")
    ep = a.endpoint or (VIDEO_ENDPOINT if a.mode == "video" else IMAGE_ENDPOINT)
    params = {"prompt": a.prompt}
    if a.mode == "video":
        params.update({"duration": a.duration, "resolution": a.resolution,
                       "aspect_ratio": a.aspect_ratio})
    if a.mode == "i2v":
        ep = a.endpoint or I2V_ENDPOINT
        img = (a.image_url or [None])[0]
        if not img:
            if not a.file:
                sys.exit("i2v needs --image-url or --file")
            img = upload_file(a.file)
        params.update({"image_url": img, "duration": a.duration,
                       "resolution": a.resolution})
    if a.mode == "r2v":
        ep = a.endpoint or R2V_ENDPOINT
        urls = list(a.image_url or [])
        if not urls and a.file:
            urls = [upload_file(a.file)]
        if not urls:
            sys.exit("r2v needs at least one --image-url (or --file)")
        params.update({"image_urls": urls, "duration": a.duration,
                       "resolution": a.resolution,
                       "aspect_ratio": a.aspect_ratio,
                       "generate_audio": (not a.no_audio)})
    if a.mode == "ms-image":
        ep = a.endpoint or MS_IMAGE_ENDPOINT
        params.update({"moderation": a.moderation,
                       "resolution": "2k" if a.resolution == "720p" else a.resolution,
                       "aspect_ratio": a.aspect_ratio,
                       "enhance_prompt": a.enhance_prompt})
        if a.quality:
            params["quality"] = a.quality
        if a.preset_id:
            params["preset_id"] = a.preset_id
        if a.image_url:
            params["image_urls"] = a.image_url

    if a.estimate_only:
        estimate(ep, params)
        return

    if a.mode == "estimate":
        estimate(ep, params)
        return

    rid = submit(ep, params)
    result = poll(rid, a.timeout)
    if result["status"] != "completed":
        sys.exit("terminal state: %s" % result["status"])
    media = result.get("images", [result.get("video", {})])[0]
    url = (media or {}).get("url")
    if not url:
        sys.exit("completed but no output url")
    day = datetime.date.today().isoformat()
    outdir = a.outdir or os.path.expanduser("~/data/higgsfield/" + day)
    ext = ".mp4" if a.mode in ("video", "i2v", "r2v") else ".jpg"
    download(url, os.path.join(outdir, rid + ext))


if __name__ == "__main__":
    main()
