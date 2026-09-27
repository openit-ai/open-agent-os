# SketchUp for Web — CDP automation reference

Everything below was verified live against `https://app.sketchup.com/app` (Chromium 151, `--headless=new`, Linux) driving the 2026-09 web modeler UI. All coordinates assume a pinned **1440×900** viewport (the scripts enforce it via `Emulation.setDeviceMetricsOverride`) and the default toolbar/menu layout.

## 0. Environment

- A Chromium with a CDP endpoint. Persistent form (recommended):

  ```
  <chrome> --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage \
    --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
    --user-data-dir=<persistent profile> about:blank
  ```

  A user systemd unit keeps it alive across sessions. Keep CDP on loopback only — it is full control of the browser (cookies, sessions) with no auth.
- Check: `curl -s http://127.0.0.1:9222/json/version`.
- The profile stores the Trimble/Google session: sign in **once** interactively (Google SSO), then headless reuse — the home screen shows previously created models.

## 1. Scripts

| script | purpose |
|---|---|
| `scripts/cdp-eval.mjs '<expr>'` | evaluate JS in the page; prints `{ok, v}` JSON (state probes) |
| `scripts/cdp-act.mjs <cmd> …` | trusted input — `click x y`, `dblclick`, `move`, `drag x1 y1 x2 y2 [steps]`, `key <Name>`, `type <text>`, `wheel x y dy` |
| `scripts/cdp-shot.mjs <out.png>` | PNG screenshot |
| `scripts/cdp-export.mjs SKP\|PNG\|STL [downloadDir]` | full menu → Download flow with download capture |

All scripts accept `CDP_TARGET=<tabId>` to pin the exact tab (REQUIRED when several tabs are open) and `CDP_URL` to override the endpoint; every script calls `Page.bringToFront` + `Target.activateTarget` before acting.

## 2. Session & page state

- List tabs: `curl -s http://127.0.0.1:9222/json/list` — pin the modeler tab's `id` into `CDP_TARGET`. `/json/list` order is not stable; without pinning, "first match wins" and you can silently drive the wrong tab.
- Modeler readiness: `document.querySelector('[data-testid="tools-pushPull"]') !== null` → modeler UI mounted (the WASM engine takes ~6 s on first load). The home screen instead shows `[data-testid="create-new-button"]`.
- Active tool probe: `[...document.querySelectorAll('[data-testid$="-selected"]')]` → e.g. `Push/Pull-selected`.
- Measurements (VCB) read-back: the only wide visible `input` at `[1229,872,183,24]` — read `.value`.
- Menu open probe: `/Download/.test(document.body.innerText)`.

## 3. Trusted-input rules

- JS `.click()` on app buttons is **silently ignored** — dispatch real input: `Input.dispatchMouseEvent` (`mouseMoved` → `mousePressed` → ~50 ms → `mouseReleased`).
- Keys: `Input.dispatchKeyEvent` keyDown(±`char`)+keyUp, with correct `code` values (`Digit3` for `3`, `KeyM` for `m`, `Comma`, `Period`, …). Tool shortcuts work when the page is focused: `r` rectangle · `p` push/pull · `o` orbit · `l` line · `m` move · `t` tape measure · `b` paint · `e` eraser · `Space` select.
- Typing into the Measurements box: plain digits/units (`5m`, `3m,4m`) + `Enter` — accepted while an operation context is active (right after a drag that created/edited geometry). The box normalizes `,` to `;` in its display (`Dimensions 3m;4m`).
- The tab must be **foreground**: in headless multi-tab, a hidden tab (`document.visibilityState === 'hidden'`) breaks UI interaction — menus never open and clicks do nothing visible.

## 4. Coordinate map (1440×900)

- **Menu button**: (22,22). Menu items (x 0–191, each 40 tall) — click center x=95:

  | item | y-center | | item | y-center |
  |---|---|---|---|---|
  | Home | 76 | | Import | 276 |
  | New | 116 | | Export (submenu →) | 316 |
  | Open | 156 | | Download (submenu →) | 356 |
  | Save as | 196 | | App Settings | 417 |
  | Share | 236 | | Add location | 457 |
  | | | | Print | 497 |

- **Export submenu** (x 191–406, click center x=298): 3DS 316 · Collada 356 · DWG 396 · DXF 436 · FBX 476 · KMZ 516 · OBJ 556 — CAD interchange formats (account/plan dependent).
- **Download submenu** (x 191–322, click center x=256): **SKP 356 · PNG 396 · STL 436**.
- **Homebar**: Undo (194,22) · model name · Redo (238,22) · … · Save/Share (right side).
- **Left toolbar** (40×40 buttons, click center x=26): search 188 · Select 242 · Eraser 282 · Line 322 · Rectangle 362 · Push/Pull 402 · Move 442 · Rotate 482 · Scale 522 · Paint 562 · Orbit 602 · Pan 642 · Tape Measure 682 · overflow 722.
- **Measurements box**: x 1229–1412, y 872–896. **Canvas**: full window (drag on it for modeling, orbit, etc.).
- **Promo banner** (when present) covers y 0–58 and overlays the menu button — click its `Close banner` at (1415,29) first.

## 5. Recipes

**(a) New model** — open `https://app.sketchup.com/app`; if the home screen shows `Create new`, click it (center ≈ (316,86) at 1440×900); wait ~6 s; verify modeler probes.

**(b) Rectangle with exact size**

1. Rectangle tool: `cdp-act.mjs click 26 362` (or key `r`).
2. Ground drag: `cdp-act.mjs drag X1 Y1 X2 Y2 14` — pick a diagonal in open space; the face is created between the endpoints.
3. `cdp-act.mjs type '3m,4m'` then `cdp-act.mjs key Enter` → VCB shows `Dimensions 3m;4m`; the face snaps exactly.

**(c) Push/Pull with exact height**

1. Push/Pull tool: `cdp-act.mjs click 26 402` (or key `p`).
2. Drag **from inside the face** (centroid — near-edge clicks miss silently) upward: `cdp-act.mjs drag CX CY CX CY-140 14`.
3. `cdp-act.mjs type '5m'` + `key Enter` → VCB `Distance 500.0 cm`; box done.

**(d) Export (recommended: `cdp-export.mjs`)**

- `SKP` → direct blob download (`Untitled.skp`, a valid SketchUp model file).
- `STL` → direct download (binary STL of the whole model).
- `PNG` → opens the **EXPORT IMAGE** dialog (size in px, transparent-background toggle, view/scene picker) — confirming `Export as PNG` (≈ (1290,871)) triggers the download.
- **Download plumbing**: call `Browser.setDownloadBehavior {behavior:'allow', downloadPath:<absolute dir>, eventsEnabled:true}` from the **same live connection** that performs the triggering clicks. When that connection closes, the behavior reverts; downloads triggered later land in the browser's default download directory. `cdp-export.mjs` bundles both in one connection.

**(e) Camera / iteration** — Orbit button (26,602) then drag sideways to rotate; Undo/Redo via homebar buttons (194,22)/(238,22) — prefer buttons over keyboard shortcuts.

**(f) Verification** — screenshot via `cdp-shot.mjs`; numeric checks via VCB read-back and, for STL, parse the binary header:

```python
import struct
d = open('model.stl','rb').read()
n = struct.unpack('<I', d[80:84])[0]          # triangle count
# each triangle: 50 bytes starting at 84; vertices = floats 3..11 (skip the normal)
# spans of a 3×4×5 m model: X 300, Y 400, Z 500 (values in cm — the model's own display unit)
```

## 6. Model persistence

- The web app **autosaves** the model to the Trimble account (`AutosaveOperation.saveModel` / `SyncFileSystem` console logs) — an untitled model survives reloads as “Untitled”.
- `Save` (homebar) forces a sync; `Save as` opens the Trimble Connect save dialog.

## 7. Pitfalls (battle-tested)

1. **Hidden tab = dead UI.** Always `Page.bringToFront` (+ `Target.activateTarget`) before any interaction, or menus won't open and clicks vanish. This was the single biggest reliability issue during development.
2. **Pin the tab** (`CDP_TARGET`) when more than one tab matches the URL — list order is not stable across sessions.
3. **Downloads are per-connection** (see 5d) — set and click within one live connection.
4. **Near-edge clicks miss.** Aim at a face's centroid; after a missed click the app is idle and letter keys become global tool shortcuts (`m` switches to Move instead of typing “m”).
5. **First screenshot after a viewport change can hang** (heavy WebGL under software rendering) — retry once; later captures are fast.
6. **Promo banner** overlays the top-left; close it before menu automation.
7. Keep every action at the pinned 1440×900 viewport — other window sizes shift the toolbar/menu geometry; re-measure via `cdp-eval.mjs` selector rects instead of reusing stale coordinates.
