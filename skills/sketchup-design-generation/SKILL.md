---
name: sketchup-design-generation
description: "Create SketchUp 3D models from chat — web or desktop MCP."
version: 0.1.0
author: OpenIT (openit-ai), Hermes Agent
license: Apache-2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [sketchup, 3d-modeling, design, mcp, browser, cdp]
    related_skills: []
---

# SketchUp Design Generation

Turn a chat request into a real SketchUp model and real files out of it. Two paths:

- **A — SketchUp for Web over CDP (verified on Linux, headless):** the agent drives `https://app.sketchup.com/app` through a Chromium DevTools endpoint — pick tools, drag geometry, type exact dimensions into the Measurements box, and export **SKP / PNG / STL** back to the machine. One interactive sign-in (Google/Trimble), kept in the browser profile for later headless runs.
- **B — Desktop SketchUp + MCP (the [SketchUp-MCP](https://github.com/mhyrr/sketchup-mcp) bridge):** a Ruby extension listens inside desktop SketchUp (`127.0.0.1:9876`) and the `sketchup-mcp` MCP server exposes modeling tools to any MCP client — create/transform components, apply materials, boolean and joinery operations, run Ruby, export scenes. Requires desktop SketchUp (Windows/macOS).

## When to Use

- "Make a 3m × 4m × 5m box / room / house in SketchUp" — anything modelable from dimensions and parts.
- Exporting a model (SKP/PNG/STL) from an automated session.
- Desktop path: SketchUp Pro on Windows/macOS wired to an MCP client (the "connect SketchUp to your AI via MCP" video method).
- Don't use for: just viewing or sharing a SketchUp file (open it in a browser instead).

## Path A — Web modeler over CDP (verified)

Prereqs: a Chromium with a CDP endpoint (e.g. `--headless=new --remote-debugging-port=9222`), signed into a Trimble account once; scripts in `scripts/` (all accept `CDP_TARGET=<tabId>` to pin the tab and force a 1440×900 viewport).

1. **Create a model** — open `https://app.sketchup.com/app`; on the home screen click **Create new**; the modeler's WASM engine takes ~6 s to mount.
2. **Pick a tool** — trusted-click its toolbar button (`[data-testid="tools-pushPull"]`, x≈26) — verify with `[data-testid$="-selected"]`.
3. **Exact dimensions are typed, not dragged** — immediately after a drag creates/edits geometry, type numbers with units and press Enter. Examples: rectangle → `3m,4m` + Enter (Measurements: `Dimensions 3m;4m`); push/pull → `5m` + Enter (Measurements: `Distance 500.0 cm`).
4. **Push/Pull** — drag from the **center** (centroid) of a face, never near an edge, then type the height.
5. **Export** — menu (22,22) → **Download** → **SKP / PNG / STL**. PNG opens an EXPORT IMAGE dialog — confirm `Export as PNG`. Keep `Browser.setDownloadBehavior` and the triggering clicks in the *same live connection* (`scripts/cdp-export.mjs` does this for you).
6. **Verify** — screenshot (`cdp-shot`) + Measurements read-back (`cdp-eval`); for STL, parse the binary header (triangle count, bounding box) to prove the dimensions numerically.

## Path B — Desktop MCP (documented)

1. **Extension** — build `su_mcp.rbz` (`cd su_mcp && zip -r ../su_mcp_vX.rbz su_mcp.rb su_mcp`) → SketchUp **Extension Manager → Install Extension** → restart.
2. **Start the server in SketchUp** — Extensions → MCP Server → **Start Server** (listens on `localhost:9876`).
3. **Run + register the MCP server** — `uvx --with 'mcp<2' sketchup-mcp` (the `mcp<2` pin is required with current MCP SDK releases — verified). Register with your MCP client; full details in `references/desktop-mcp.md`.
4. **Ask for models** — tools: `create_component` (cube/cylinder/sphere/cone with position & dimensions), `transform_component`, `set_material`, `boolean_operation`, `create_mortise_tenon` / `create_dovetail` / `create_finger_joint`, `eval_ruby`, `export_scene` (skp/obj/dae/stl/png — files land in `<TEMP>/sketchup_exports/`).

## Pitfalls (battle-tested)

- **Headless multi-tab: a hidden tab's UI is dead.** Menus won't open and clicks vanish until the tab is brought to front — call `Page.bringToFront` + `Target.activateTarget` before any interaction (the scripts do). This was the single biggest reliability issue during development.
- **Downloads are per-connection**: `Browser.setDownloadBehavior` applies only while the CDP client that set it stays connected — set it and click in the same live connection or files land in the browser's default download folder.
- **JS `.click()` is ignored by the app** — dispatch real input (`Input.dispatchMouseEvent`). Keep the viewport pinned to 1440×900 so coordinates stay valid.
- **A promo banner can cover the top-left menu button** — dismiss it (`Close banner`) before menu automation.
- **Near-edge face clicks miss silently** — then letter keys become global tool shortcuts (`m` → Move). Aim at the centroid.
- **The first screenshot after a viewport change can hang** (software-rendered WebGL) — retry once; later captures are fast.

## Files

| file | purpose |
|---|---|
| `scripts/cdp-eval.mjs` | evaluate JS in the page — state probes (active tool, Measurements value) |
| `scripts/cdp-act.mjs` | trusted input — click / dblclick / move / drag / key / type / wheel |
| `scripts/cdp-shot.mjs` | PNG screenshot |
| `scripts/cdp-export.mjs` | full Download → SKP/PNG/STL flow with download capture |
| `references/web-automation.md` | the detailed web recipe — selectors, full coordinate map, export plumbing, pitfalls |
| `references/desktop-mcp.md` | the desktop MCP bridge — extension build, server, tool catalog, caveats |

Build story, verified numbers, and step-by-step usage: `README.md`.
