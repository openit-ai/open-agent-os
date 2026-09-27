---
name: figma-design-generation
description: "Generate and edit Figma designs via the Figma MCP server."
version: 0.1.0
author: OpenIT (openit-ai), Hermes Agent
license: Apache-2.0
platforms: [linux]
metadata:
  hermes:
    tags: [figma, design, mcp, ui, diagram, design-to-code]
    related_skills: []
---

# Figma Design Generation

Generate, edit, and review Figma designs (screens, frames, components, diagrams) from a chat request, through the official Figma remote MCP server (`https://mcp.figma.com/mcp`). Flow: **connect check → load official skill → acquire file → generate → screenshot review → iterate → deliver**.
The Hermes MCP client authenticates to Figma with OAuth 2.0 (dynamic client registration is handled automatically); the assistant model writes `use_figma` scripts and Figma executes them inside the file.

## When to Use

- "Build/create this in Figma" — screens, frames, wireframes, components, diagrams.
- Editing an existing Figma file; capturing a live web page into Figma (code → design); pulling design context into code (design → code).
- Don't use for: local image generation (use `codex-image` if installed); documents or slide decks (use `google-office-maker` if installed).

## Prerequisites

- A Figma account (Google SSO works). The remote MCP works on the free Starter plan; read-class tools are rate-limited (below).
- Hermes with the Figma MCP server configured: `hermes mcp install figma` (catalog entry; endpoint `https://mcp.figma.com/mcp`, OAuth 2.0), then `hermes mcp login figma` once in a browser. The token is stored at `~/.hermes/mcp-tokens/figma.json` (~90 days); re-run the login command when it expires.
- Verify: `hermes mcp test figma` → Connected, 40 tools discovered.
- Rate limits (official): read-class tools only — Starter **20/month** (any seat), View/Collab seats on paid plans 6/month, Dev/Full seats 200/day (10–15/min), Enterprise 600/day. Write-class tools (`create_new_file`, `use_figma`, `add_code_connect_map`, …) are **exempt**. Keep read calls (screenshots, metadata) deliberate.
- Full guide (structure, verified results, step-by-step): [README.md](./README.md).

## Principles (5 verified rules)

1. **Load the official skill first** — read the `resource:figma-use` skill before every `use_figma` call and list it in the `skillNames` parameter. It carries the rules that prevent the common hard-to-debug failures (colors 0–1, font loading, `return`, page handling).
2. **Target, don't rewrite** — modify the node the user points at (by id or name); work in the file that already holds the design instead of rebuilding it.
3. **Screenshot before claiming done** — one `get_screenshot` after a pass; fix a single variable at a time.
4. **Respect the quota split** — write tools are exempt, read tools are limited; report read-call consumption with the deliverable.
5. **Editable output** — screenshots are reference only; the deliverable must be editable layers (text, components, hierarchy), never a flattened image of the whole UI.

## Procedure

### P1 — Acquire the file

- New file: `whoami` (take `plans[].key`) → `create_new_file` (`planKey` is required).
- Existing file: take the file URL / file key.

### P2 — Generate

- `use_figma` — JavaScript (Figma Plugin API) executed inside the file. Snippets and the rules digest: `references/tools-and-prompts.md`.
- Diagrams: `generate_diagram` (flowchart, decision tree, gantt, sequence, state, ERD).
- Live web page → Figma: `generate_figma_design`. Assets: `upload_assets` / `download_assets`.

### P3 — Review

- `get_screenshot` on the target node (visual check). Structure: `get_metadata`. Design → code: `get_design_context`.

### P4 — Iterate

- One change → re-screenshot → stop once the request is satisfied (no busy loops).

### P5 — Deliver

- File URL + screenshot + a short summary of applied changes (+ design context on request).

## Pitfalls

- Long JS code passed through a chat model can be mangled by JSON escaping (observed) — keep scripts short and simple; treat an empty `return` as a failure and retry, or call the MCP directly for deterministic runs.
- `create_new_file` without `planKey` fails schema validation — resolve it via `whoami`.
- Editing text without loading its font first throws (`Cannot write to node with unloaded font`).
- Page context resets to the first page on every `use_figma` call — fan multi-page work into parallel calls (one page per call).
- Read-quota burnout on Starter (20/month) — don't spray screenshots/metadata calls.
- Honor the tools' "You MUST load figma-*" instructions by reading the matching MCP skill resource.

## Verification

- `hermes mcp test figma` succeeds; the end-to-end path (whoami → create/update → screenshot) is exercised on a live account.
- Deliver only after a visual check of the screenshot; report read-quota usage.
