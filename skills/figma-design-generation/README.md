# figma-design-generation — skill guide

> Generate, edit, and review Figma designs from a chat request through the official Figma remote MCP server — connect once, then drive designs, diagrams, and design-to-code handoffs conversationally.
> This document records how the skill was built, what has been verified, and how to run it step by step.

- **Version**: 0.1.0 · **Platform**: Linux (Hermes Agent)
- **Location (installed)**: `~/.hermes/skills/figma-design-generation/`
- **MCP server**: `https://mcp.figma.com/mcp` — official Figma remote MCP, OAuth 2.0

**How to read this document**
- What it does → §1 · Structure → §2 · Background → §3
- Prerequisites — accounts & limits → §4
- Verified results → §5 · Step-by-step usage → §6 · Troubleshooting → §7

---

## 1. What it does

Pipeline: **connect check → load official skill → P1 acquire file → P2 generate → P3 screenshot review → P4 iterate → P5 deliver**

| Stage | Tool | Role |
|---|---|---|
| Brain — request → script | the assistant model in Hermes | Writes and corrects `use_figma` scripts |
| Execution — files & design | Figma remote MCP | `create_new_file` · `use_figma` · `generate_diagram` · `generate_figma_design` |
| Review | `get_screenshot` | Visual check of the rendered result |
| Connection | Hermes MCP client | OAuth 2.0 (dynamic client registration) handled automatically |

**The 5 verified rules**

1. **Load the official skill first** — `resource:figma-use` before every `use_figma` call (colors 0–1, font loading, `return`, page handling).
2. **Target, don't rewrite** — modify the node the user points at; keep working in the file that already holds the design.
3. **Screenshot before claiming done** — one `get_screenshot` per pass; fix a single variable at a time.
4. **Respect the quota split** — write tools are exempt from rate limits; read tools (screenshots, metadata) are limited.
5. **Editable output** — screenshots are reference only; the deliverable is editable layers, never a flattened image of the whole UI.

## 2. Structure

```
figma-design-generation/
├── SKILL.md                        # the runbook Hermes loads
├── README.md                       # this document
└── references/
    └── tools-and-prompts.md        # 40-tool inventory, official resources, snippets
```

## 3. Background

- Built on the **official Figma remote MCP server** (`https://mcp.figma.com/mcp`, OAuth 2.0). Figma accepts connections only from clients listed in the Figma MCP Catalog; the Hermes MCP client registers dynamically (DCR) and connects — confirmed by the provider's login-page branding during authorization.
- OAuth flow verified: `hermes mcp login figma` → authorize URL → browser login + consent → localhost callback → **"✓ Authenticated — 40 tool(s) available"**. The token lives at `~/.hermes/mcp-tokens/figma.json` (~90 days, refresh included); re-run the login command when it expires.
- The server exposes 40 tools grouped as: file/generation (`use_figma`, `create_new_file`, `generate_diagram`, `generate_figma_design`, `upload_assets`, `download_assets`), read/context (`get_screenshot`, `get_design_context`, `get_metadata`, `get_variable_defs`, `get_motion_context`, `get_figjam`, `get_libraries`, `search_design_system`, `export_video`), Code Connect (6), Weave workflow runs (9), shader/generative-plugin management (9), and `whoami`. The server also ships official agent skills and docs as MCP resources (e.g. `skill://figma/figma-use/SKILL.md`) — the skill teaches loading them before writing scripts.

## 4. Prerequisites — accounts & limits

- **Figma account** (Google SSO works). The remote MCP works on the free Starter plan — verified.
- **Rate limits** (official `rate-limits-access`, verified 2026-09) — read-class tools only:
  - Starter: **20/month** (any seat) · View/Collab seats on paid plans: 6/month
  - Dev/Full seats: Pro·Org 200/day (10–15/min), Enterprise 600/day (20/min)
  - **Exempt**: write-class tools — `create_new_file`, `whoami`, `add_code_connect_map`, and generally tools that write to files.
- **Cost**: none beyond your Figma plan.

## 5. Verified results (2026-09)

- `hermes mcp install figma` registered the server; `hermes mcp test figma` → **Connected (895 ms) · 40 tools discovered**.
- OAuth login completed end-to-end on a live **Starter** account → 40 tools authorized.
- `whoami` returned live account data (plans → the `planKey` source for file creation).
- `create_new_file` created a design file; `use_figma` drew a 400×300 blue frame with a centered white label ("Hermes connected"); `get_screenshot` returned the rendered 400×300 PNG — visual check passed.

## 6. Step-by-step usage

Install:

```bash
hermes skills install openit-ai/open-agent-os/skills/figma-design-generation
```

Connect the MCP server (once):

```bash
hermes mcp install figma      # registers the catalog entry
hermes mcp login figma        # one-time browser login
hermes mcp test figma         # Connected · 40 tools
```

**Start** — say "Build ⟨something⟩ in Figma". The skill loads and the loop runs: acquire file → generate (`use_figma` / `generate_diagram`) → screenshot review → iterate → deliver URL + screenshot.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| tool call 401 / auth error | token expired or missing | `hermes mcp login figma` |
| Tools missing in a session | tools load at session start | open a new session / reload MCP |
| `create_new_file` schema error | `planKey` missing | resolve it via `whoami` |
| Permission error on a file | account lacks access to that file | run `whoami`; check the file's plan/permissions |
| Rate limit hit | read tools limited (Starter 20/month) | wait, use write-class tools, or upgrade the seat/plan |
| `use_figma` returns "no return value" | long code mangled in transit | keep scripts short, retry, or call the MCP directly |
| Headless server login blocked | automation detected | apply UA/viewport/WebGL overrides over CDP before login; keep a signed-in browser session; popups may need separate handling |

## 8. References

- Figma MCP server docs: https://developers.figma.com/docs/figma-mcp-server/ (tools-and-prompts · rate-limits-access · remote-server-installation)
- Figma MCP Catalog: https://www.figma.com/mcp-catalog/
- Hermes MCP docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp
- In-skill: `references/tools-and-prompts.md`

---
Part of the [open-agent-os](https://github.com/openit-ai/open-agent-os) repository · License: Apache-2.0
