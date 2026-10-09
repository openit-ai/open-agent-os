---
name: vps-youtube-content
description: "Use when YouTube transcript fetch is blocked on a VPS."
version: 0.1.0
author: OpenIT (openit-ai), Hermes Agent
license: Apache-2.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [youtube, transcript, captions, bot-wall, vps]
    related_skills: [youtube-content, blocked-page-recovery]
---

# VPS YouTube Content

VPS/cloud-hosted variant of YouTube transcript fetching. Use when the standard transcript fetch fails behind an IP/bot wall — the common case on datacenter egress IPs. Companion to `youtube-content` (which formats transcripts into summaries, chapters, threads): recover the transcript here, then hand it to that workflow — or summarize and format directly when `youtube-content` is unavailable (this skill replaces it on hosts where the default fetch is blocked). For page-style blocks (paywall, WAF, 403) use `blocked-page-recovery` instead.

## When to Use

- `youtube-content`'s transcript fetch (`youtube-transcript-api`) reports an IP/request block.
- yt-dlp fails with `Sign in to confirm you're not a bot`.
- Any task pulling YouTube captions from a VPS or cloud-server egress IP.

## Diagnose before acting

YouTube blocks by egress-IP reputation: a request failing from a cloud-server IP usually succeeds from a home connection. Confirm which failure you have, because only a reputation block is worth working around — genuinely missing subtitles have no workaround:

- Transcript API error naming IP/request blocking → reputation block.
- yt-dlp `Sign in to confirm you're not a bot` → reputation block.
- Empty result with no error → subtitles likely disabled; stop here.

DNS changes do nothing — the block is not DNS. Do not loop the same failing call; each attempt costs time and deepens rate-limiting.

## The ladder (cheapest first, stop when a step's verdict is final)

### 1. Alternate yt-dlp player clients

```bash
python -m yt_dlp --skip-download --write-auto-subs --sub-langs "ko,en" \
  --extractor-args "youtube:player_client=android" --print title "<URL>"
```

Rotate `android`, then `ios`, then `tv`. Cheap and sometimes enough on soft-flagged IPs. If all three return the bot-check error, the IP is hard-flagged — client rotation will not save it.

### 2. PO Token provider

The canonical community fix for flagged-IP bot checks: a pip-installable yt-dlp plugin plus a small Node token server that mints proof-of-origin tokens over BotGuard (upstream: `Brainicism/bgutil-ytdlp-pot-provider`).

```bash
pip install bgutil-ytdlp-pot-provider yt-dlp
# clone the provider repo at its latest release tag, then in server/:
npm ci && npx tsc
node build/main.js --port 4416   # binds loopback only; keep it there
```

The plugin registers itself with yt-dlp automatically (verify with `--verbose`: the log lists the `bgutil:http` PO Token provider). Then rerun the step-1 command unchanged — no extra flags needed.

A token does not guarantee bypass: a hard-flagged IP still fails with a valid token attached. That verdict is final for the host — do not retry with more token servers or flags.

### 2B. Credential-free reader-proxy fetch

A reader-proxy fetches the watch page from its own unflagged egress IP and returns the transcript as markdown — no login, no cookies, no approval needed:

```bash
curl -s -m 40 "https://defuddle.md/https://www.youtube.com/watch?v=<ID>" -o subs.md
```

Validate the body (a transcript section with timestamped segments), not the status/size — caption endpoints can return HTTP 200 with an empty body. Send nothing credential-bearing through the proxy; it only ever sees a public video URL.

### 3. User-decided routes only

Past step 2, every remaining route needs explicit user approval because each carries cost or risk:

- User-provisioned residential egress IP (datacenter/VPN IPs share the same flagged ranges, so only residential works).
- Cookie auth (`--cookies-from-browser` / `--cookies`) — risks a permanent ban on the account used. Never do this without explicit approval. Cookies must come from a fresh session (exported within ~30 minutes) with a matching full browser User-Agent; stale exports fail even when the login behind them is still valid.
- Fetch from a home machine on a residential connection and paste the text.
- Download the media once (when reachable) and transcribe locally with STT.

## Dead ends — do not present these as options

- Public Invidious instances as caption proxies: most are dead or gated. Validate the response body before treating one as a route.
- Generic web-proxy relays for fetching: unverifiable provenance by construction; never send credentials through one.
- Retrying the identical failing command hoping the block lifts: it does not lift within a session, and repeated hits worsen rate-limiting.

## Verification

- A route works only when the returned body contains the transcript itself (timestamped segments) — validate content, not status codes or sizes.
- Record which rung succeeded and the verbatim error from the failing baseline, so the ladder stays honest about what was actually blocked.
