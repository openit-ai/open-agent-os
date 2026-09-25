# higgsfield-media-generation — skill guide

> Generate images and videos through the Higgsfield API from Hermes Agent — estimate → submit → poll → download, with a zero-dependency helper script (Python stdlib only).
> This document records how the skill was built, what has been verified, and how to run it step by step.

- **Version**: 0.1.0 · **Platform**: Linux (Hermes Agent)
- **Location (installed)**: `~/.hermes/skills/higgsfield-media-generation/`
- **Companion skill**: `promo-video-generation` — a 20-second promo pipeline built on this helper

**How to read this document**
- What it does → §1 · Structure → §2 · Background → §3
- Prerequisites — accounts & costs → §4
- Verified results → §5 · Usage → §6 · Troubleshooting → §7

---

## 1. What it does

A thin, auditable wrapper around the Higgsfield REST API (docs verified 2026-09-18):

| Mode | Purpose |
|---|---|
| `image` | Text-to-image — default `higgsfield-ai/soul/v2/standard` |
| `ms-image` | Marketing Studio image — presets, moderation, prompt enhance |
| `video` | Text-to-video — default `bytedance/seedance-2.5/text-to-video` |
| `r2v` | Reference-to-video — up to 30 reference images, keeps the real scene (`bytedance/seedance-2.5/reference-to-video`) |
| `i2v` | Image-to-video — `kling-video/v3.0-turbo/image-to-video`; `--file` auto-uploads |
| `upload` | Upload local files → public URL for use as references |
| `presets` | List Marketing Studio presets (never hardcode preset IDs) |
| `status` | Poll a submitted request |
| `estimate` / `--estimate-only` | Pre-flight cost quote with the exact parameters |

Design rules: **estimate first — never submit without a quote** · **server-side only** (credentials stay in the server environment; never call from browser/client code) · **stdlib only** (no pip dependencies) · **polling only** (no webhook or batch infrastructure).

## 2. Structure

```
higgsfield-media-generation/
├── SKILL.md               # the runbook Hermes loads
├── README.md              # this document
├── references/
│   └── request-form.md    # generation request form — model, format, budget cap
└── scripts/
    └── hf_generate.py     # the helper — Python stdlib only
```

## 3. Background

- Built against the official Higgsfield API docs (auth header, async lifecycle, file uploads, errors); endpoint shapes re-checked against the live API.
- `r2v` / `i2v` modes were added to serve the `promo-video-generation` pipeline (live-action still → video), along with `--estimate-only` and `--no-audio`.
- Polling (2s start, 1.5× backoff, 10s cap) was chosen over webhooks so the helper stays stateless and works behind NAT.
- Known pitfall handled in-script: Cloudflare error `1010` — Python's default User-Agent is blocked before reaching the API, so the script sends a browser UA.

## 4. Prerequisites — accounts & costs

- **Higgsfield API account (paid, pay-as-you-go)** — the API is a separate product from the higgsfield.ai website: a website plan does not grant API access. Create an account, add a payment method, top up a USD balance, and create an API key in the [Higgsfield Console](https://console.higgsfield.ai) — the key is shown once at creation, so store it securely.
- **Server-side credentials** — `HF_API_KEY_ID` / `HF_API_KEY_SECRET` in the server environment (e.g. `.env`, mode 600). Never in repos, docs, or chat; never call the API from browser or mobile code.
- **Billing model** — no subscription; each successful generation deducts its cost from the balance and spending stops at zero. Failed generations are refunded automatically. Video is priced per second of output (Seedance 2.5 from $0.0738/s, Kling 3.0 from $0.112/s) and images per image (from ~$0.0032); see the pricing page for current rates. Top-up funds expire one year after they are added.
- **Output retention** — generated files are kept for a minimum of 7 days; download outputs to your own storage for anything long-term.
- **Companion pipeline** — `promo-video-generation`, which runs on this helper, additionally requires a Codex CLI sign-in: a ChatGPT account; regular use needs a paid plan or an API key with billing (see its README).

## 5. Verified results (2026-09)

- Auth header (`Authorization: Key <ID>:<SECRET>`) verified against the official docs.
- Estimate → submit → poll → download lifecycle exercised; sample quotes measured: Seedance 2.5 R2V 20s · 720p ≈ $9.24; Kling 3.0 Turbo i2v 10s $0.616 → $0.504 (45% off).
- `python -m py_compile` clean; runs with no third-party packages.
- Installed via `hermes skills install` (community path): SAFE scan → installed, all support files fetched.

## 6. Usage

```bash
hermes skills install openit-ai/open-agent-os/skills/higgsfield-media-generation
```

```bash
HF=$HOME/.hermes/skills/higgsfield-media-generation/scripts/hf_generate.py
# Free quote first — same parameters as the real run
python3 $HF --mode r2v --estimate-only --prompt "..." --image-url <u1> --image-url <u2> \
  --duration 20 --resolution 720p --aspect-ratio 16:9
# Submit → the script polls (2s → 10s backoff) → downloads to ~/data/higgsfield/<date>/
python3 $HF --mode r2v --prompt "..." --image-url <u1> --image-url <u2> \
  --duration 20 --resolution 720p --aspect-ratio 16:9
# Status check
python3 $HF --mode status --request-id <id>
```

Workflow rule: **quote → approval → submit → report the actual final cost**. Run long jobs (600s+) in the background with notifications.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 Invalid credentials` | bad or missing key ID / secret | re-check the environment values; regenerate in the Console |
| `403` with Cloudflare error `1010` | default Python User-Agent blocked | send a browser UA (the script handles this) — not a billing issue |
| Terminal state `failed` / `nsfw` | content policy | adjust the prompt — not billed (auto-refund) |
| Quote returns a description instead of a number | token-based endpoints | treat as advisory; compute from the published formula |
| Output URL stops working | 7-day retention window passed | download outputs immediately after completion |
| Upload rejected | unsupported file type | supported: jpg / jpeg / png / webp / gif / wav / mp4 |

## 8. References

- Higgsfield API docs: https://docs.higgsfield.ai
- Higgsfield Console: https://console.higgsfield.ai
- Pricing: https://higgsfield.ai/pricing
- Companion skill: `promo-video-generation`

---
Part of the [open-agent-os](https://github.com/openit-ai/open-agent-os) repository · License: Apache-2.0
