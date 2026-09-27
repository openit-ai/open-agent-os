# Figma MCP — tool inventory, official resources, snippets

Server: `https://mcp.figma.com/mcp` (official Figma remote MCP, OAuth 2.0). Measured inventory: **40 tools** (`hermes mcp test figma`, 2026-09).
Limits: **read-class tools only** are rate-limited; write-class tools are exempt — see §4.

## 1. Tools (40), grouped

### File & generation (write — exempt from rate limits)
| Tool | Purpose |
|---|---|
| `create_new_file` | Create a new Design/FigJam/Slides file — **requires `planKey`** (from `whoami`) |
| `use_figma` | Create/edit/design-sync — runs JavaScript on the Figma Plugin API (load the official skill first) |
| `generate_figma_design` | Capture a live web page into an existing Figma file (code → design) |
| `generate_diagram` | Flowchart, decision tree, gantt, sequence, state, ERD |
| `upload_assets` / `download_assets` | Upload images/SVGs / download a node's render, source images, and vectors |

### Read & context (rate-limited)
`get_screenshot` (review loop core) · `get_design_context` (design → code) · `get_metadata` (structure; prefer get_design_context) · `get_variable_defs` (design tokens) · `get_motion_context` (keyframes) · `get_figjam` (FigJam → UI code) · `get_libraries` · `search_design_system` · `export_video` (timeline node → MP4)

### Code Connect (code ↔ design mapping)
`get_code_connect_map` · `get_code_connect_suggestions` · `add_code_connect_map` (exempt) · `send_code_connect_mappings` · `get_context_for_code_connect` · `list_file_components_for_code_connect`

### Weave (workflow & model runs — 9)
`weave_list_tools` · `weave_get_tool_inputs` · `weave_run_tool` · `weave_upload_asset` · `weave_get_tool_run_output` · `weave_cancel_tool_run` · `weave_find_model` · `weave_run_model` · `weave_get_model_run_output`

### Shader & generative plugins (9 — each requires its official skill)
`list_shaders` · `get_shader` · `create_shader` · `update_shader` · `list_file_shaders` / `list_generative_plugins` · `get_generative_plugin` · `create_generative_plugin` · `update_generative_plugin`

### Account
`whoami` (exempt) — handle, email, plans (the `planKey` source)

## 2. Official skills & docs (MCP resources)

Read via MCP `read_resource`. Record the skill in `use_figma`'s `skillNames` as `resource:<name>` (logging only).

**Skills** (`skill://figma/<name>/SKILL.md`):
- `figma-use` — mandatory prerequisite for `use_figma` (0–1 colors, font loading, `return` rules)
- `figma-generate-design` — code/description → screens and views (reuse the design system, assemble section by section)
- `figma-create-new-file` — `create_new_file` procedure (resolving `planKey`)
- `figma-generate-library` — components, variants, token foundations
- `figma-generate-diagram` — per-type diagram guidance (architecture, ERD, flowchart, gantt, sequence, state, workflow)
- further: `figma-use-figjam`, `figma-use-slides`, `figma-use-motion`, `figma-code-connect`, `figma-design-to-code`, `figma-implement-motion`, `figma-swiftui`, `figma-shaders`, `figma-generative-plugins` + many `references/` files (e.g. `figma-use/references/api-reference.md`, `gotchas.md`)

**Docs** (`file://figma/docs/<name>.md`):
`tools-and-prompts` · `rate-limits-access` · `write-to-canvas` · `remote-server-installation` · `write-effective-prompts` · `structure-figma-file` · `variables-vs-code` · `code-connect-integration` · `mcp-vs-agent` · `trigger-specific-tools` · `stuck-or-slow` and more.

## 3. `use_figma` snippets

### Frame + label (verified end-to-end)
```js
const frame = figma.createFrame();
frame.name = "Hermes Test";
frame.resize(400, 300);
frame.x = 100; frame.y = 100;
frame.fills = [{ type: "SOLID", color: { r: 0.11, g: 0.33, b: 0.91 } }];
const text = figma.createText();
await figma.loadFontAsync({ family: "Inter", style: "Regular" });
text.characters = "Hermes connected";
text.fontSize = 28;
text.fills = [{ type: "SOLID", color: { r: 1, g: 1, b: 1 } }];
frame.appendChild(text);
text.x = (400 - text.width) / 2;
text.y = (300 - text.height) / 2;
return { createdNodeIds: [frame.id, text.id], frameId: frame.id };
```

### Upsert an existing node by name (verified)
```js
const frame = figma.currentPage.children.find(n => n.name === "Hermes Test");
if (!frame) { return { error: "not found" }; }
frame.resize(400, 300);
frame.fills = [{ type: "SOLID", color: { r: 0.11, g: 0.33, b: 0.91 } }];
const text = frame.children.find(n => n.type === "TEXT");
if (text) { text.x = (400 - text.width) / 2; text.y = (300 - text.height) / 2; }
return { frameId: frame.id, textId: text ? text.id : null };
```

### Rules digest (official `figma-use` — the usual violations)
- Colors are **0–1** `{r,g,b}` (no `a` — opacity goes at the paint level). Fills are read-only arrays → clone, modify, reassign.
- Text: load the font → mutate → **`return` node IDs**. `console.log` is not returned.
- Prefer auto-layout (`figma.createAutoLayout`) for containers with structural child relationships.
- Page switching: `await figma.setCurrentPageAsync(page)` — at most once per call; fan multi-page work into parallel calls.
- Keep new top-level nodes away from (0,0). Code is auto-wrapped in an async context — do not use an IIFE.

### Code delivery tip
- Long code passed through a chat model can get truncated by JSON escaping (observed: "Code executed with no return value"). Keep scripts short/simple and verify the `return` value.

## 4. Read/write split (rate limits)
- **Exempt (official)**: `create_new_file`, `whoami`, `add_code_connect_map`, and generally tools that write to files.
- **Limited**: read-class tools — per seat/plan: Starter 20/month (any seat); View/Collab on paid plans 6/month; Dev/Full seats 200/day (10–15/min); Enterprise 600/day (20/min). Source of truth: `file://figma/docs/rate-limits-access.md`.
