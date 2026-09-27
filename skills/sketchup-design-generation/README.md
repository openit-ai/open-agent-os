# sketchup-design-generation — skill guide

> Create SketchUp 3D models from chat and export real files (SKP/PNG/STL) — by driving SketchUp for Web (`app.sketchup.com`) in a headless browser over CDP, or by bridging desktop SketchUp with the open SketchUp-MCP server.
> This document records how the skill was built, what has been verified, and how to run it step by step.

- **Version**: 0.1.0 · **Platform**: Linux (verified) · macOS/Windows share the concepts
- **Location (installed)**: `~/.hermes/skills/sketchup-design-generation/`
- **Verified against**: headless Chromium 151 (`--headless=new`) + CDP 9222, web modeler UI 2026-09

**How to read this document**
- What it does → §1 · Structure → §2 · Background → §3
- Prerequisites — accounts & environment → §4
- Verified results (measured) → §5 · Step-by-step usage → §6 · Troubleshooting → §7

---

## 1. What it does

Pipeline (web path): **session → Create new → (tool · drag · typed dimension) × N → review (screenshot · read-back) → Download (SKP/PNG/STL) → file verification**

| Stage | Tool | Role |
|---|---|---|
| Brain — request → modeling plan | the assistant model in the host agent | Designs dimensions, parts, and the step sequence |
| Execution — web modeler | `scripts/cdp-*.mjs` (trusted CDP input) | Tool selection, drags, Measurements input, export |
| Review | `cdp-shot` + `cdp-eval` read-back | Visual check + Measurements value check |
| (Desktop) Execution | SketchUp-MCP (`uvx sketchup-mcp` + Ruby extension) | Components, materials, booleans, joinery, Ruby, scene export |

**The five rules that make it reliable**

1. **Activate the tab first** — `Page.bringToFront` (+ `Target.activateTarget`) before any interaction; hidden headless tabs have dead UI (menus never open).
2. **Pin the tab** — `CDP_TARGET=<tabId>`; never rely on "first match" when several tabs are open.
3. **Pin the viewport** — 1440×900 for every action; coordinates below are for that size.
4. **Exact dimensions are typed** — drag approximate geometry, then type `3m,4m` / `5m` + Enter into the Measurements box.
5. **Downloads: same-connection rule** — `Browser.setDownloadBehavior` and the triggering clicks must share one live CDP connection.

## 2. Structure

```
sketchup-design-generation/
├── SKILL.md                        # the runbook the agent loads
├── README.md                       # this document (build record & verified results)
├── references/
│   ├── web-automation.md           # selectors, full coordinate map, recipes, pitfalls
│   └── desktop-mcp.md              # extension, MCP server, tool catalog, caveats
└── scripts/
    ├── cdp-eval.mjs                # state probes (evaluate JS)
    ├── cdp-act.mjs                 # trusted input (click/drag/key/type/wheel)
    ├── cdp-shot.mjs                # screenshot
    └── cdp-export.mjs              # Download → SKP/PNG/STL with download capture
```

## 3. Background

- Starting point: the public video “2 Ways to Connect Astra and SketchUp via MCP” and the community bridge [mhyrr/sketchup-mcp](https://github.com/mhyrr/sketchup-mcp). The video's manual path = install uv → Python 3.12 → install the `.rbz` extension → edit the MCP client config.
- Host constraint: the automation host has no desktop SketchUp (Linux), so a second path was developed and verified: **SketchUp for Web automation** — one interactive sign-in, then headless reuse of the stored session.
- Two decisive pitfalls were found and fixed during the build: **hidden tabs have dead UI** (`Page.bringToFront` required) and **`Browser.setDownloadBehavior` only applies while the setting connection stays alive** (set and click in one connection, or downloads fall back to the default folder).
- The desktop MCP server was verified at the source level (PyPI `sketchup-mcp` 0.1.17): tool dispatch traced through `src/sketchup_mcp/server.py` + `su_mcp/main.rb`, and `uvx` runs pass with the `mcp<2` pin.

## 4. Prerequisites

- **Web path**: a Trimble account (free tier works) signed in once via Google; a Chromium with a CDP endpoint (`--headless=new --remote-debugging-port=9222 --user-data-dir=<profile>` — keep CDP on loopback only).
- **Desktop path**: desktop SketchUp (Windows/macOS) + [uv](https://docs.astral.sh/uv/) for the MCP server. No added cost — SketchUp licensing is separate.
- Node.js for the scripts (stdlib-only CDP WebSocket client; no npm dependencies).

## 5. Verified results (measured)

2026-09, headless Chromium 151 · 1440×900 · CDP 9222:

| Check | Result |
|---|---|
| Model creation (Create new → modeler) | WASM engine mounts in ~6 s; toolbar probes pass |
| Rectangle 3 m × 4 m | drag → `3m,4m` + Enter → Measurements `Dimensions 3m;4m` |
| Push/Pull 5 m | face-centroid drag → `5m` + Enter → Measurements `Distance 500.0 cm` — **3 m × 4 m × 5 m box complete** |
| SKP export | `Untitled.skp`, **191,074 B** — `file` reports “SketchUp Model” |
| PNG export | EXPORT IMAGE dialog → `Export as PNG` → **1440×900 PNG**, 18,068 B |
| STL export | `Untitled.stl`, **146,734 B** = 84 + 50 × **2,933** triangles — bounding box **Δ300.0 × Δ400.0 × Δ500.0 (Z 0–500)**, exactly 3 m × 4 m × 5 m in the model's unit |
| MCP server (desktop path) | `uvx sketchup-mcp`, `uvx --with 'mcp<2' sketchup-mcp`, and the git+URL form all run clean |
| Session persistence | Signed-in profile survives restarts (home lists prior models); models autosave (Autosave/SyncFileSystem console logs) |

## 6. Step-by-step usage

### A. Web modeler (primary path)

```bash
# 0) CDP endpoint + tab id
curl -s http://127.0.0.1:9222/json/version
curl -s http://127.0.0.1:9222/json/list      # take the app.sketchup.com tab's id → CDP_TARGET

# 1) State probe (modeler mounted, active tool)
cd <skill>/scripts
export CDP_TARGET=<tabId>
node cdp-eval.mjs 'document.querySelector("[data-testid=tools-pushPull]") !== null'

# 2) Rectangle 3m×4m → Push/Pull 5m
node cdp-act.mjs click 26 362                # Rectangle tool
node cdp-act.mjs drag 430 500 830 720 14     # drag on the ground
node cdp-act.mjs type '3m,4m'; node cdp-act.mjs key Enter
node cdp-act.mjs click 26 402                # Push/Pull tool
node cdp-act.mjs drag 425 624 425 484 14     # drag from the face centroid, upward
node cdp-act.mjs type '5m'; node cdp-act.mjs key Enter

# 3) Review + export
node cdp-shot.mjs out.png
node cdp-export.mjs SKP ./downloads          # or PNG / STL
```

The full 1440×900 coordinate map lives in `references/web-automation.md` §4; re-measure selector rects with `cdp-eval.mjs` if the layout ever shifts.

### B. Desktop MCP (on a machine with SketchUp installed)

1. Build & install the `.rbz`: `cd su_mcp && zip -r ../su_mcp_vX.rbz su_mcp.rb su_mcp` → Extension Manager → Install Extension → restart.
2. In SketchUp: Extensions → MCP Server → **Start Server**.
3. Register the MCP server (generic JSON clients): see `references/desktop-mcp.md`.
4. Prompts: “Create a 3 m × 4 m × 5 m box”, “Make the selection red”, “Export the scene as STL”.

## 7. Troubleshooting

| Symptom | Cause → fix |
|---|---|
| Top-left menu won't open / clicks do nothing | Hidden tab — `Page.bringToFront` (+`Target.activateTarget`); ensure you run the current scripts |
| Download lands in the default folder | The connection that set `Browser.setDownloadBehavior` closed — do set+click in one connection (use `cdp-export.mjs`) |
| Clicked but nothing happened | Coordinates from another tab or viewport — pin `CDP_TARGET`, keep 1440×900; click face centroids, not edges |
| Typing switches tools instead of typing | No operation context (the drag missed) — only type after a drag actually created geometry |
| First screenshot hangs | Viewport change + software-rendered WebGL — retry once |
| PNG export produced no file | The EXPORT IMAGE dialog needs its own `Export as PNG` confirmation (≈ (1290,871)) |
| `uvx sketchup-mcp` fails | Add the `--with 'mcp<2'` pin (current MCP SDK compatibility) |
| Desktop: no connection after Start Server | Port 9876 busy or double-start — Stop Server, restart, keep a single MCP server process |

Full battle log: `references/web-automation.md` §7.
