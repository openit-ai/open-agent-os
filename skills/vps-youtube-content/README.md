# VPS YouTube Content

Fetch YouTube transcripts from VPS or cloud hosts when YouTube's IP / bot wall blocks the standard path.

## Why this exists

Plain `youtube-transcript-api` fetches and vanilla yt-dlp runs are routinely blocked from datacenter egress IPs (`Sign in to confirm you're not a bot`, IP/request-block errors) — the everyday situation on a VPS. Where `youtube-content` turns a transcript into summaries, chapters and threads, this skill gets you the transcript itself when the fetch is blocked.

## What it does

- **Diagnose before acting** — IP-reputation block (worth working around) vs missing subtitles (stop) vs DNS myths (irrelevant). Don't grind the same failing call.
- **Work the recovery ladder, cheapest first** — rotate yt-dlp player clients → PO Token provider (`bgutil-ytdlp-pot-provider`) → credential-free reader-proxy fetch → user-decided routes only (residential IP, explicit-approval cookie auth, home-machine fetch, local STT).
- **Stay honest** — a rung only counts when the response body actually contains the transcript; a hard-flagged host verdict is final, with no retry loops.

## Install

```bash
hermes skills install openit-ai/open-agent-os/skills/vps-youtube-content
```

## Field notes

- The skill originated from a live KVM-VPS bot-wall incident; the credential-free reader-proxy rung was re-verified from a VPS egress on 2026-09-27.
- Use with the `youtube-content` skill (bundled with Hermes) for the content workflow after recovery.
- For page-style blocks (paywall, WAF, 403) see the `blocked-page-recovery` skill instead.
