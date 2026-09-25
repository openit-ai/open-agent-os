# promo-video-generation — skill guide

> Create a 20-second live-action promo video for a place (store, museum, campus, stadium, …) from just its name — driven entirely through chat.
> This document records how the skill was built, what has been verified, and how to run it step by step.

- **Version**: 0.1.0 · **Platform**: Linux (Hermes Agent)
- **Location (installed)**: `~/.hermes/skills/promo-video-generation/`
- **Companion skill**: `higgsfield-media-generation` — provides the generation helper (`scripts/hf_generate.py`)

**How to read this document**
- What it does → §1 · Structure → §2 · Background → §3
- Prerequisites — accounts & costs → §4
- Verified results → §5 · Step-by-step usage → §6 · Troubleshooting → §7

---

## 1. What it does

Pipeline: **Q&A → P1 facts & symbols → P2 live-action images → P3 shot script → P4 generation (quote → approval) → P5 review & delivery**

| Stage | Tool | Role |
|---|---|---|
| Brain — script, prompts, checks | Codex CLI (a strong GPT6-class model) | Writes image prompts and the shot script |
| Images — live-action stills | Codex `image_gen` (no separate image API) | Generates and retouches the stills |
| Video — final generation | Higgsfield API — Seedance 2.5 R2V (default) / Kling 3.0 i2v (budget) | Reference-to-video with music & effects |
| Conversation | Hermes chat — Q&A guide | Locks the brief in 6 questions |

**The 4 verified rules**

1. **Images before video** — never prompt straight to video. Build consistent live-action stills first, then animate them. No script, no images → no submission.
2. **Exact logos** — names/logos appear where they physically are. Never restate lettering inside a video prompt (the model redraws it and it collapses); refer to "image 2, the entrance sign" instead.
3. **Photoreal feel** — "a photo with the small imperfections of reality". No overdone HDR or plastic gloss; keep the real light direction.
4. **One continuous music bed** — one instrumental track runs 0→20s with no stop or restart at cuts. No human voices (dialogue or narration).

## 2. Structure

```
promo-video-generation/
├── SKILL.md               # the runbook Hermes loads
├── README.md              # this document
└── references/
    └── templates.md       # image & video prompt templates + worked examples
```

## 3. Background

Adapted from public prompting guides ("GPT6 Astra X Seedance 2.5" and "Artlist X Astra") to this toolchain.

| Original guide | This skill | Why |
|---|---|---|
| Conversational image generation | Codex `image_gen` | One toolchain; no separate image API |
| "Seedance 2.5 R2V · 1080p" | Higgsfield API `bytedance/seedance-2.5/reference-to-video` | API-automatable; same model family |
| 1080p | 720p | Seedance API caps at 480/720p — upscaling is a separate step |
| Manual upload & polling | `hf_generate.py --mode r2v` (upload → poll → download automated) | Unattended runs |
| Prose director prompt | 4 rules + 6-question interview + P1–P5 + templates | Chat-driven (Q&A) workflow |

## 4. Prerequisites — accounts & costs

- **Codex CLI (paid-plan usage)** — sign in with a ChatGPT account. Codex is included across ChatGPT plans with plan-based limits; regular use needs a paid plan (see chatgpt.com/pricing) or an OpenAI API key with billing. It powers the drafting brain and the image path.
- **Higgsfield API (paid, pay-as-you-go)** — the video step runs on the Higgsfield API: a USD balance topped up in advance, no subscription. A higgsfield.ai website plan does not grant API access. Create an account and API key in the Higgsfield Console (console.higgsfield.ai). Credential setup: see the companion skill README.
- **Cost reference** (2026-09 quotes): Seedance 2.5 R2V 20s · 720p ≈ **$9.24** (pre-discount) · Kling 3.0 Turbo i2v 10s ≈ **$0.616 → $0.504** (45% off). Failed generations are refunded automatically.
- **Approval rule** — always quote first; nothing is submitted without explicit approval; regenerations stay within the approved scope.

## 5. Verified results (2026-09)

- Codex brain smoke-tested (prompt & script drafting); the image path exercised end-to-end (1024×1024 output verified).
- Higgsfield R2V quote measured with exact 20s/720p parameters; Kling i2v quote measured with the discount applied.
- Installed via `hermes skills install` (community path): SAFE scan → installed, all support files fetched.

## 6. Step-by-step usage

Install:

```bash
hermes skills install openit-ai/open-agent-os/skills/promo-video-generation
```

**Start** — say "Make a promo video for ⟨place⟩". The skill loads and the interview begins.

**Interview (one question at a time; offer defaults):**

| # | Question | Default |
|---|---|---|
| 1 | Which place? (name, branch) | required |
| 2 | Do you have photos? | if none, collect from the web |
| 3 | Logo / signage? official logo file? | fall back to real photos |
| 4 | Format: 20s · 16:9 · 720p? | yes — the Seedance API caps at 480/720p |
| 5 | Model: Seedance 2.5 (R2V) / Kling 3.0 (i2v)? | Seedance R2V |
| 6 | Quote → approval → generate | approval required — never submit without a quote |

**P1 — Facts & symbols**: search "⟨place⟩ symbol / landmark / photo spot"; whatever repeats across the official site, wiki, blogs, and maps = the symbols. Pick 2–3, collect real photos with source URLs. Pin down where the logo/signage physically appears.

**P2 — Live-action images (16:9)**: user photos → web photos → generate missing shots with Codex `image_gen` (referenced to real photos). Templates in `references/templates.md`. Give every image the same grade and time of day so the film reads as one piece.

**P3 — Shot script**: one-line story → ≈10 cuts (1–3s each, 20s total), ≥5 distinct locations, ≥5 camera moves. Rows: `[cut · seconds] | [image ref] | [subject · action] | [camera move] | [transition]`. Sound paragraph: one continuous track, no dialogue.

**P4 — Generate (quote → approve → submit):**

```bash
HF=$HOME/.hermes/skills/higgsfield-media-generation/scripts/hf_generate.py
# 1) free quote with the exact parameters → user approval
python3 $HF --mode r2v --estimate-only --prompt "..." --image-url <u1> --image-url <u2> \
  --duration 20 --resolution 720p --aspect-ratio 16:9
# 2) submit (make images public first: --mode upload --file <path>)
python3 $HF --mode r2v --prompt "<shot script>" --image-url <u1> --image-url <u2> \
  --duration 20 --resolution 720p --aspect-ratio 16:9
```

Polling and download are automatic. No duplicate submissions; no regeneration outside the approved scope.

**P5 — Review & deliver**: check the result (warped props, identity drift) → fix one variable per regeneration (cost approval each time). Deliver: video + images used + full prompts + photo/logo sources.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Codex call rejected with 403 | routing points at an external provider | override the provider for that call (`-c model_provider="..."`) |
| websocket 426 warning | proxy without websocket support | harmless — the call completes over HTTP fallback |
| Quote returns a description instead of a number | token-based endpoint | treat as advisory; compute from the published formula |
| Logo / lettering collapses | lettering written inside the prompt | refer to "image N, the entrance sign" instead |
| 1080p request fails | Seedance API caps at 480/720p | generate at 720p; upscale separately |
| Video `failed` / `nsfw` | content policy | adjust the prompt — not billed (auto-refund) |

## 8. References

- Higgsfield API docs: https://docs.higgsfield.ai — Seedance 2.5, reference-to-video, Kling 3.0 model pages
- Higgsfield Console: https://console.higgsfield.ai
- Companion skill: `higgsfield-media-generation`

---
Part of the [open-agent-os](https://github.com/openit-ai/open-agent-os) repository · License: Apache-2.0
