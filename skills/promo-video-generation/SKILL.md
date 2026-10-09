---
name: promo-video-generation
description: "Create 20-second live-action promo videos for places."
version: 0.1.0
author: OpenIT (openit-ai), Hermes Agent
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [video, promo, place, seedance, kling, higgsfield, codex]
    related_skills: [higgsfield-media-generation]
---

# Promo Video Generation

Create a 20-second live-action promo video for a place (store, museum, campus, stadium, …) from just its name. Flow: **Q&A → facts & symbols → live-action images → shot script → generation → review & delivery**.
Brain = Codex CLI (a strong model — e.g. a GPT6-class model in your setup); images = Codex `image_gen` (no separate image API); video = Higgsfield API (Seedance 2.5 R2V default / Kling 3.0 budget fallback).
Adapted from public prompting guides ("GPT6 Astra X Seedance 2.5" and "Artlist X Astra") to this toolchain.

## When to Use

- "Make a promo video for ⟨place⟩" — store / museum / campus / stadium intros.
- Don't use for: story or character creative videos (use `seedance-prompt-craft` if installed); standalone image generation (use `codex-image` if installed).

## Prerequisites

- `higgsfield-media-generation` skill installed — it provides `scripts/hf_generate.py` and the API credential setup.
- Codex CLI signed in with a ChatGPT account (the drafting brain and the image path). Codex is included across ChatGPT plans with plan-based usage limits; regular use needs a paid plan (see chatgpt.com/pricing) or an OpenAI API key with billing.
- Higgsfield API account with a topped-up balance — paid, pay-as-you-go (no subscription; a higgsfield.ai website plan does not grant API access). Keys are created in the Higgsfield Console (console.higgsfield.ai); see the companion skill README.
- The user is present: the interview and every cost approval happen in their chat.
- Full guide (structure, verified results, step-by-step usage): [README.md](./README.md).

## Principles (4 verified rules)

1. **Images before video** — never prompt straight to video. Build consistent live-action images first, then animate them. No script, no images → no submission.
2. **Exact logos** — names/logos appear where they physically are. Never restate lettering inside the video prompt (the model redraws it and it collapses); refer to "image 2, the entrance sign". Only approach a logo in letter-free shots.
3. **Photoreal feel** — "a photo with the small imperfections of reality". No overdone HDR or plastic gloss; keep the real light direction. Never drop this phrase.
4. **One continuous music bed** — one new instrumental track runs 0→20s with no stop or restart at cuts. No human voices (dialogue or narration).

## Q&A guide (chat interview — one question at a time)

Offer a default with each question; if the user says "your call", proceed with the default. Run the interview in the user's language.

| # | Question | Default |
|---|---|---|
| 1 | Which place? (name, branch) | required |
| 2 | Do you have photos? | if none, collect from the web |
| 3 | Logo / signage? official logo file? | fall back to real photos |
| 4 | Format: 20s · 16:9 · 720p? | yes — the Seedance API caps at 480/720p |
| 5 | Model: Seedance 2.5 (R2V) / Kling 3.0 (i2v)? | Seedance R2V |
| 6 | Quote → approval → generate | approval required — never submit without a quote |

## Procedure

### P1 — Facts & symbols
- Search "⟨place⟩ symbol / landmark / photo spot" → whatever repeats across the official site, wiki, blogs, and maps = the symbols. Pick 2–3, collect real photos, record each source URL.
- Pin down where the logo/signage physically appears (entrance, signboard); get the official logo original if it exists.

### P2 — Live-action images (16:9)
- Priority: ① user photos → ② web photos → ③ generate missing shots with Codex `image_gen` (referenced to real photos).
- Prompt templates: `references/templates.md`. Logo shots: retouch the real photo only. No face/hand close-ups; keep recurring people consistent.
- Give every image the same grade and time of day so the film reads as one piece.

### P3 — Shot script
- One-line story → ≈10 cuts (1–3s each, 20s total), ≥5 distinct locations, ≥5 camera moves.
- Cut rows: `[cut · seconds] | [image ref] | [subject · action] | [camera move] | [transition]`.
- Sound paragraph: one continuous track + effects, no dialogue. Template: `references/templates.md`.
- Drafts can be written and checked with the Codex CLI model.

### P4 — Generate (Higgsfield)
```bash
HF=$HOME/.hermes/skills/higgsfield-media-generation/scripts/hf_generate.py
# 1) free quote with the exact parameters → get user approval
python3 $HF --mode r2v --estimate-only --prompt "..." --image-url <u1> --image-url <u2> \
  --duration 20 --resolution 720p --aspect-ratio 16:9
# 2) submit (make images public first: --mode upload --file <path>)
python3 $HF --mode r2v --prompt "<shot script>" --image-url <u1> --image-url <u2> \
  --duration 20 --resolution 720p --aspect-ratio 16:9
```
- Cost reference (2026-09-25 quotes): R2V 20s · 720p = 432,000 tokens ≈ **$9.24** (pre-discount, image refs only) · Kling 3.0 Turbo i2v 10s ≈ **$0.616 → $0.504** at 45% off — budget mode = silent Kling clips.
- Polling and download are automatic. No duplicate submissions; no regeneration outside the approved scope.

### P5 — Review & deliver
- Check the result (warped props, identity drift) → fix one variable per regeneration (cost approval each time).
- Deliver: video + images used + full prompts + photo/logo sources. If the audio is quiet, provide a posting copy (−14 LUFS / −1 dB).

## Codex CLI usage (the drafting brain)

- The brain is any strong Codex CLI model — pick one available in your setup (a GPT6-class model works well). If your routing needs a provider override, pass it per call (`-c model_provider="..."`); keep such environment details out of this file.
- Non-interactive drafting: `codex exec -s read-only "<image prompt / shot-script request>"`.
- Image generation: `codex exec -s workspace-write "<image_gen request>"` (no separate image API; details in `codex-image` if installed).

## Pitfalls

- Straight to video (top failure); lettering written inside prompts (logo collapse).
- Too many simultaneous camera instructions; repeating the same symbol (not enough variety); counting a zoom-in on the same photo as a new scene.
- Approaching logos in shots (letters get redrawn); long face/hand close-ups (deformation).
- Submitting without a quote; regenerating without approval.
- Asking for 1080p (the Seedance API caps at 720p) — upscaling is a separate step.

## Verification

- Do not enter generation before the shot script + sound paragraph + image set + sources exist.
- Order: quote → approval → submit; report the actual final cost.
- Confirm the delivered video plays (length, resolution); ship prompts and sources alongside.
