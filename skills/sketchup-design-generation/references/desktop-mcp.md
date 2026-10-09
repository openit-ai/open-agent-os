# SketchUp desktop + MCP bridge (SketchUp-MCP)

Bridge **desktop SketchUp** to any MCP client. Upstream: [mhyrr/sketchup-mcp](https://github.com/mhyrr/sketchup-mcp) (PyPI `sketchup-mcp`, MIT). Requires desktop SketchUp (Windows/macOS) — this path does not apply to SketchUp for Web.

Two pieces:

1. **SketchUp extension** (`su_mcp.rb` + `su_mcp/` — Ruby): runs an in-app TCP server on `127.0.0.1:9876`. Menu: **Extensions → MCP Server → Start/Stop Server**.
2. **MCP server** (`src/sketchup_mcp/server.py` — Python): speaks MCP to the client (FastMCP) and forwards tool calls to the extension socket (default `localhost:9876`).

## Install

Extension — build a `.rbz` from source (a `.rbz` is a zip with `su_mcp.rb` **and** the `su_mcp/` folder at the top level):

```bash
git clone https://github.com/mhyrr/sketchup-mcp
cd sketchup-mcp/su_mcp
zip -r ../su_mcp_vX.Y.Z.rbz su_mcp.rb su_mcp
```

Then in SketchUp: **Extension Manager → Install Extension** → select the `.rbz` → restart SketchUp.

MCP server (Python ≥ 3.10, [uv](https://docs.astral.sh/uv/) installed):

```bash
uvx --with 'mcp<2' sketchup-mcp
```

The `mcp<2` pin is required with current MCP SDK releases — verified working: `uvx sketchup-mcp`, `uvx --with 'mcp<2' sketchup-mcp`, and `uvx --from 'git+https://github.com/mhyrr/sketchup-mcp' --with 'mcp<2' sketchup-mcp`.

MCP client config (generic JSON clients):

```json
{ "mcpServers": { "sketchup": { "command": "uvx", "args": ["--with", "mcp<2", "sketchup-mcp"] } } }
```

Hermes:

```bash
hermes mcp add sketchup --command uvx --args "--with mcp<2 sketchup-mcp"
```

## Run

1. Start SketchUp with the model open.
2. **Extensions → MCP Server → Start Server** (log line “Server started and listening”; console at window `SKETCHUP_CONSOLE`).
3. Ensure the MCP server process runs (the client launches it on demand).
4. Prompt the agent: “Create a 3 m × 4 m × 5 m box”, “Make the selection red”, “Export the scene as STL”.

## Tool catalog (source-verified: `src/sketchup_mcp/server.py` + `su_mcp/main.rb`)

| tool | params | notes |
|---|---|---|
| `create_component` | `type` (`cube` / `cylinder` / `sphere` / `cone`), `position [x,y,z]`, `dimensions [w,h,d]` | component primitives |
| `delete_component` | `id` | |
| `transform_component` | `id`, `position` / `rotation` (deg) / `scale` | |
| `get_selection` | — | current-selection report (the upstream README calls this `get_selected_components` / `get_scene_info`; current source exposes `get_selection`) |
| `set_material` | `id`, `material` | material name or color keyword (red/green/blue/yellow/cyan/magenta/white/black/brown/orange/gray …) |
| `export_scene` | `format`: `skp` · `obj` · `dae` · `stl` · `png`/`jpg` | writes `sketchup_export_<timestamp>.<ext>` under `<TEMP>/sketchup_exports/` |
| `boolean_operation` | union / difference / intersection | via the Ruby dispatcher |
| `chamfer_edges` · `fillet_edges` | edge params | |
| `create_mortise_tenon` · `create_dovetail` · `create_finger_joint` | joint dimensions | woodworking joinery helpers |
| `eval_ruby` | `code` | arbitrary Ruby inside SketchUp — advanced, unsandboxed |

## Manual setup (the reference video's detailed path)

The video “2 Ways to Connect Astra and SketchUp via MCP” walks two options:

1. **Easy path** — ask the assistant to install everything (it edits the client config for you).
2. **Manual path** — install `uv`, let uv provide Python 3.12, install the `.rbz` via Extension Manager, then hand-edit the MCP client config (`~/.codex/config.toml` in the video; any client works — see the JSON above). With Hermes, `hermes mcp add` replaces the manual config edit.

Both converge on the same two pieces documented here.

## Caveats

- Desktop SketchUp only; the extension drives the Ruby API (SketchUp 2017+ era APIs; video demo used SketchUp 2025).
- One client connection at a time to port 9876 — restart the extension server if the socket is stuck.
- Exported files land in the OS temp directory — copy them to a durable location.
- `eval_ruby` is powerful and unsandboxed: run only code you trust.
