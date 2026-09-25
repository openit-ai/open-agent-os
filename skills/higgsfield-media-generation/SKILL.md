---
name: higgsfield-media-generation
description: "Generate images and videos via the Higgsfield API."
version: 0.1.0
author: OpenIT (openit-ai), Hermes Agent
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [higgsfield, media, image, video, seedance, kling]
    related_skills: [promo-video-generation]
---

# Higgsfield Media Generation

Generate images and videos through the Higgsfield API. Jobs follow an async lifecycle: submit → poll → download. Reference: https://docs.higgsfield.ai/docs (verified 2026-09-18).

## When to Use

- Image/video generation via the Higgsfield API; status checks; pre-flight cost estimates.
- New briefs: collect them with the request form (`references/request-form.md`) so model, format, and budget cap are explicit.
- Don't use for: local model rendering; other generation APIs.

## Prerequisites

- Higgsfield API account (paid, pay-as-you-go — no subscription; a higgsfield.ai website plan does not grant API access). Create an account, top up a USD balance, and create an API key in the Higgsfield Console (console.higgsfield.ai) — the key is shown once, so store it securely.
- `HF_API_KEY_ID` and `HF_API_KEY_SECRET` set in the server environment (e.g. `.env`, mode 600) — never in repos, docs, or chat.
- Helper: `scripts/hf_generate.py` (stdlib only, no dependencies) — run it via `terminal`.
- Endpoints — image: default `higgsfield-ai/soul/v2/standard`, brand/marketing `marketing-studio/image`. Video: default `bytedance/seedance-2.5/text-to-video`; reference-to-video `bytedance/seedance-2.5/reference-to-video` (`--mode r2v`, repeat `--image-url`, up to 30 images); image-to-video `kling-video/v3.0-turbo/image-to-video` (`--mode i2v`, `--file` auto-uploads). Kling family selectable via `--endpoint`.
- Full guide (structure, verified results, accounts & signup, step-by-step usage): [README.md](./README.md).

## Procedure

1. Report on every call: ① quote the cost and get user approval before submitting; ② report the actual final cost after completion. Never submit without an estimate. Completion: one approval before + one report after.
2. Submit. Image: `--mode image --prompt ...`. Video: `--mode video --prompt ... --duration 5 --resolution 720p --aspect-ratio 16:9`. Reference-to-video: `--mode r2v --prompt ... --image-url <u1> --image-url <u2> --duration 20 --resolution 720p --aspect-ratio 16:9` (`--estimate-only` quotes with the same parameters). Completion: `request_id` received.
3. The script polls for you (2s start, 1.5× backoff, 10s cap; terminal states `completed | failed | nsfw | canceled`). For long jobs (600s+), run in the background with notify. Completion: terminal state reached.
4. Download outputs immediately — result URLs are kept for 7 days. Save under `~/data/higgsfield/<date>/` and deliver with `MEDIA:`. Completion: file received + delivered.
5. Failure handling: `401` → stop and report credentials; `failed | nsfw` → not billed (auto-refund); cancel works only before processing starts. Completion: branch handled per state.

## Pitfalls

- Never call from browser or client-side code (official warning) — server-side only.
- Video costs are large (multi-dollar for 15s at 720p) — never submit without an estimate.
- Image-to-video runs with `--mode i2v`; with `--file` it auto-uploads → public URL → generates.
- Marketing Studio: enhance mode needs a preset + one product image (+10% cost). Never hardcode preset UUIDs — list them with `--mode presets` first.
- Use the actual returned output URL, not the example domain.
- 403 `error code: 1010` is a Cloudflare block, not a billing problem. Python's default User-Agent is blocked before reaching the API — a browser UA is required (the script handles this). Do not misdiagnose it as balance.
- Operating policy: single jobs on demand — no webhook or batch infrastructure; polling only.
- Avoid surprise results: start with a cheap proof (silent, 720p, shortest) to lock direction, then run the real generation. (360p / framerate knobs do not exist on these models.)

## Verification

- Estimate vs. actual charge reported (pre-approval + post-report).
- Output file bytes + playback (resolution, length) checked before delivery.
- Zero key/secret exposure in logs or reports.
