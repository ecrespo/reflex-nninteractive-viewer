# reflex-nninteractive-viewer

A [Reflex](https://reflex.dev) component that wraps the medical image viewer of
[nnInteractive Agent](https://github.com/MIC-DKFZ/nninteractive-agent) (MIC-DKFZ): three slice
views (axial, coronal, sagittal) and a ray-cast 3D view, with an instance overlay, a prompt
overlay, live object surfaces, CT presets, and click-to-voxel events back to Python.

It reproduces the left pane of the nnInteractive Agent web demo, so a Reflex app can reuse
`nninteractive_agent` (`AgentWorkspace`, `web/layers.py`, the agent runner) as its backend.

## What it wraps

The upstream viewer is framework-free JavaScript, and this package wraps it rather than rewriting it:

| File (in `src/reflex_nninteractive_viewer/assets/`) | Origin | Role |
|---|---|---|
| `viewer.js` | upstream `web/static/viewer.js` (+ a documented `destroy()` patch) | `Viewer(els, callbacks)`: one NiiVue canvas with a custom tile layout, the 3D canvas, all mouse handling |
| `render3d.js` | upstream, unmodified | `Render3D(canvas)`: WebGL2 volume ray casting with object surfaces composited by depth |
| `coords.js` | upstream, unmodified | workspace voxel ↔ NiiVue RAS voxel conversions (exact "mention location") |
| `vendor/niivue-0.69.0.esm.js` | upstream vendored copy of NiiVue 0.69.0, unmodified | slice rendering |
| `nni_viewer.jsx` | this package | the React wrapper |

The upstream commit, the checksums and the one patch are recorded in
[`assets/PATCHES.md`](https://github.com/ecrespo/reflex-nninteractive-viewer/blob/main/src/reflex_nninteractive_viewer/assets/PATCHES.md).

## Architecture

```
 Python (Reflex state)                    Browser
 ─────────────────────                    ───────────────────────────────────────────────────────────────
                         props (URLs and          nni_viewer.jsx (one useEffect per prop)     viewer.js
                         metadata only)
 volume_meta  ─────────────────────────────────▶  resolves relative URLs ───────────────────▶ load(meta)
 objects      ─────────────────────────────────▶  ──────────────────────────────────────────▶ setObjects / setHidden
 meshes       ─────────────────────────────────▶  diff by key/version/url/color ────────────▶ setMesh / removeMesh
 patch_url    ─────────────────────────────────▶  fetch bundle when objects change ─────────▶ applyPatch × n
 patch_ws_url ─────────────────────────────────▶  WebSocket, binary frames ─────────────────▶ applyPatch
 preset       ─────────────────────────────────▶  ──────────────────────────────────────────▶ setVolume3d
 window       ─────────────────────────────────▶  ──────────────────────────────────────────▶ setWindow
 surface_opacity ──────────────────────────────▶  ──────────────────────────────────────────▶ setSurfaceOpacity
 overlay_mode ─────────────────────────────────▶  ──────────────────────────────────────────▶ setOverlayMode
 layout       ─────────────────────────────────▶  ──────────────────────────────────────────▶ setLayout
 tool         ─────────────────────────────────▶  ──────────────────────────────────────────▶ setTool
 focus        ─────────────────────────────────▶  ──────────────────────────────────────────▶ focusVoxel
 focus_object ─────────────────────────────────▶  ──────────────────────────────────────────▶ focusObject
                                                   arrows / PgUp / PgDn / R ─────────────────▶ stepSlice / resetView

                         events                                                    callbacks
 on_location(voxel, mm, value) ◀──────────────  click without drag + affine ◀───────────────── onLocation
 on_slice_change(voxel)        ◀──────────────  debounced ◀──────────────────────────────────── onLocation
 on_layout_request(mode)       ◀──────────────  ◀───────────────────────────────────────────── onLayoutRequest
 on_window_change(lo, hi, preset) ◀───────────  (final values only) ◀────────────────────────── onWindowChange
 on_ready(info)                ◀──────────────  load() resolved
 on_error(message)             ◀──────────────  init() / load() / fetch failed (e.g. no WebGL2)

                                  GET /api/volume/{key}/{name}   (image.nii.gz, image3d.nii.gz, zeros_*.nii.gz)
 reflex_nninteractive_viewer      GET /api/mesh/{key}            (mesh blob)
 .server (FastAPI, mounted   ◀──  GET /api/patch/{key}           (patch bundle)
 with api_transformer)            WS  /api/patch_ws/{key}        (patches, live)
                                  GET /api/img/{name}            (the agent's PNG views)
```

Image data never travels through Reflex state. The viewer downloads it from the routes.

## Install

```bash
pip install reflex-nninteractive-viewer        # or: uv add reflex-nninteractive-viewer
```

This needs Python 3.11+ and Reflex 0.10.1 or later. `fastapi` is installed for the server helpers.
`nninteractive_agent` is optional and isn't on PyPI. To use the helpers that read an
`AgentWorkspace`, install it from source:

```bash
uv pip install "nninteractive-agent[web] @ git+https://github.com/MIC-DKFZ/nninteractive-agent"
```

## Usage

```python
import reflex as rx
from reflex_nninteractive_viewer import nni_viewer
from reflex_nninteractive_viewer.server import ViewerStore, create_api

store = ViewerStore("/var/lib/myapp/viewer")  # one sub-directory per image key


class S(rx.State):
    meta: dict | None = None  # the volume_meta prop (see below)
    objects: list[dict] = []
    meshes: list[dict] = []
    layout: str = "grid"
    voxel: list[int] = []

    @rx.event
    def located(self, voxel: list[int], mm: list[float], value: float):
        self.voxel = voxel  # e.g. hand it to the agent as "the user means here"

    @rx.event
    def relayout(self, mode: str):
        self.layout = mode


def index():
    return rx.box(
        nni_viewer(
            volume_meta=S.meta,
            objects=S.objects,
            meshes=S.meshes,
            patch_url="api/patch/case1",
            layout=S.layout,
            on_location=S.located,
            on_layout_request=S.relayout,
        ),
        height="80vh",
    )


app = rx.App(api_transformer=create_api(store))
app.add_page(index)
```

### With nninteractive_agent as the backend

```python
from reflex_nninteractive_viewer import server

meta = server.write_image(store, ws.image, key)  # layers.write_volumes + URLs
store.set_patch_bundle(key, server.workspace_patch_bundle(ws))  # LayerSync patches
for oid in ws.objects:
    blob = server.workspace_mesh(ws, oid)  # layers.crop_mesh
    if blob:
        v = store.set_mesh(f"o{oid}", blob)
        # -> meshes prop: {"key": f"o{oid}", "url": f"api/mesh/o{oid}?v={v}", "version": v, "color": ...}
objects = server.workspace_objects(ws)  # colors from core.render.object_color
```

While the agent works, push each `LayerSync().patches(ws)` message with
`store.publish(key, patch)` and pass `patch_ws_url=f"api/patch_ws/{key}"`.

### Props

| Prop | Type | viewer.js | Notes |
|---|---|---|---|
| `volume_meta` | `dict \| None` | `load(meta)` | The `image` message of the upstream web session: `write_volumes()`' dict (`shape`, `spacing`, `dtype`, `is_2d`, `is_ct`, `orientation`, `auto_window`, `range`) plus `key`, `url` and `url3d`. A new `key`, `n` or `url` loads a new image. `server.volume_meta()` builds it. |
| `objects` | `list[dict]` | `setObjects`, `setHidden` | `{"id", "name", "color": "#rrggbb", "hidden", "current"}` |
| `meshes` | `list[dict]` | `setMesh`, `removeMesh`, `r3.setMeshOpacity` | `{"key", "url", "version", "color", "name", "opacity"}`. Unchanged key/version/url/color are not refetched. Key `o<id>` follows the visibility of object `id`. |
| `patch_url` | `str \| None` | `applyPatch` | A patch bundle, refetched whenever `objects` changes ("simple" mode) |
| `patch_ws_url` | `str \| None` | `applyPatch` | A WebSocket of single patches (streaming mode), reconnecting with backoff |
| `api_base` | `str \| None` | | Base for relative URLs (default: the Reflex backend) |
| `preset` | `str \| None` | `setVolume3d` | CT: `Bones`, `Bones + skin`, `Skin`, `Off`. Others: `Translucent`, `MIP`, `Off`. |
| `window` | `int \| None` | `setWindow` | Index into CT: `Soft tissue`, `Liver`, `Lung`, `Bone`, `Brain`, `Full range`. Others: `Auto`, `Full range`. |
| `surface_opacity` | `float \| None` | `setSurfaceOpacity` | 0..1 |
| `overlay_mode` | `"fill" \| "outline" \| "off"` | `setOverlayMode` | |
| `layout` | `"grid" \| "axial" \| "coronal" \| "sagittal" \| "3d" \| "single"` | `setLayout` | |
| `tool` | `"navigate" \| "window" \| "scroll" \| "pan" \| "zoom"` | `setTool` | Left button. Right-drag is always window/level, wheel scrolls slices, Ctrl+wheel zooms. |
| `focus` | `list[int] \| None` | `focusVoxel` | Workspace voxel `[axis0, axis1, axis2]` |
| `focus_object` | `int \| None` | `focusObject` | |
| `keyboard` | `bool` | `stepSlice`, `resetView` | Arrows / Page Up / Page Down / R while the viewer has focus (default on) |

Literal values are validated in Python (`ValueError` on an unknown layout, an opacity outside
0..1, a meta dict without `url`, …). Values bound to state are passed through.

### Events

| Event | Arguments | When |
|---|---|---|
| `on_location` | `voxel: list[int], mm: list[float], value: float` | A click without dragging in a slice view ("Mention location"). `voxel` is in workspace order (converted with `coords.js`). `mm` is RAS from the image's NIfTI affine. |
| `on_slice_change` | `voxel: list[int]` | The crosshair moved (wheel, keys, clicks, focus). Debounced to 150 ms. |
| `on_ready` | `info: dict` | The image is displayed. `{"windows": [...], "presets": [...], "layout", "preset"}` |
| `on_error` | `message: str` | No WebGL2, or a download failed |
| `on_layout_request` | `mode: str` | A tile label was clicked or a view double-clicked. Set `layout` to honour it. |
| `on_window_change` | `lo: float, hi: float, preset: int \| None` | A window preset was applied (`preset` = index), or a window/level drag ended (`None`) |

## Demo

The demo needs no GPU (software WebGL works), no LLM and no nnInteractive. Like the fake session
in upstream's `tests/web_dev.py`, it writes a synthetic CT phantom, sphere objects and sphere
surfaces in exactly the upstream formats (`nni_viewer_demo/nni_viewer_demo/synthetic.py`).

```bash
uv sync
cd nni_viewer_demo
uv run reflex run            # http://localhost:3000
```

What you can do in it:

- Load the volume.
- Toggle and focus objects.
- Add an object while the viewer is open. This sends a new patch bundle, a WebSocket patch and a new surface.
- Switch the 3D preset, window, layout, overlay mode, tool and surface opacity.
- Click a voxel and see `voxel`, `mm` and `value` arrive in Python state.

## Binary formats

All of these are written unchanged by `nninteractive_agent.web.layers`. `server.py` has
dependency-free encoders for the same formats (`encode_patch`, `full_layer_patch`,
`bundle_patches`, `encode_mesh`).

**Workspace order.** Workspace arrays are `[axis0, axis1, axis2]`, the reversed NIfTI `(i, j, k)`.
The C-order bytes of a workspace array are therefore NIfTI voxel order. Voxels in props and events are
in workspace order.

**Volumes** (`GET {prefix}/volume/{key}/{name}`): gzipped NIfTI-1 files as `write_volumes` writes them.

| File | Content |
|---|---|
| `image.nii.gz` | Full-resolution image for the slice views (uint8, int16, uint16 or float32) |
| `image3d.nii.gz` | Image for the 3D view, possibly downsampled (`shrink3d`). For CT the table is removed. |
| `zeros_uint8.nii.gz`, `zeros_uint16.nii.gz` | Empty label volumes with the image's header. The viewer finds them by replacing `image.nii.gz` in `url`. |

`viewer.js` reads the sform (or the qform when the sform code is 0), datatype, scaling and dimensions.

**Patch** (one label update, `viewer.applyPatch`), `layers.encode_patch`:

```
uint32 LE   header length H
H bytes     JSON header {"layer": "objects"|"current"|"prompts", "box": [[lo,hi]]*3,
                         "dtype": "uint8"|"uint16", "shape": [n0,n1,n2], "full": bool, "seq": int}
rest        zlib (deflate) of the C-order bytes of the crop  arr[lo0:hi0, lo1:hi1, lo2:hi2]
```

The `objects` layer holds object ids, and switches to uint16 above 255 objects. `current` is the
object being edited (0/1). `prompts` holds the prompt overlay: 1 = positive and 2 = negative
scribbles, boxes and points (`layers.prompts_layer`).

**Patch bundle** (`GET {prefix}/patch/{key}`, the `patch_url` prop): patches concatenated, each
prefixed with its `uint32 LE` length. `bundle_patches(LayerSync().patches(ws))` gives the full
state.

**Patch stream** (`WS {prefix}/patch_ws/{key}`, the `patch_ws_url` prop): one patch per binary frame.

**Mesh** (`GET {prefix}/mesh/{key}`, `layers.crop_mesh`). The route answers 204 when the mesh is gone.

```
uint32 LE   n_vertices
uint32 LE   n_triangles
float32 LE  vertices × 3   (RAS mm, the NIfTI world space)
uint32 LE   indices  × 3   (counter-clockwise seen from outside)
```

## Server helpers and security

`create_api(store, prefix="/api", authorize=None)` returns a FastAPI app for
`rx.App(api_transformer=...)`. Use `create_router(...)` to add the routes to your own FastAPI app.
Keys are restricted to `[A-Za-z0-9_.-]` and can't start with `.`. Volume names are restricted to
the four files above.

The routes are public unless you pass `authorize(request_or_websocket, key) -> bool` (sync or
async). Medical images are sensitive, so check the user's session there for anything beyond a
local demo, and use unguessable keys (`secrets.token_urlsafe`). A WebSocket subscriber that
falls 256 patches behind is disconnected, and resyncs through `patch_url` when it reconnects. The store lives in
memory, so run the Reflex backend as a single process.

## Limitations

- **WebGL2 is required.** Without it the viewer shows a message and fires `on_error`. Remote
  desktop sessions and VMs without a GPU often lack WebGL2, or are too slow for the 3D view. Headless
  Chromium works with SwiftShader (`--use-angle=swiftshader --enable-unsafe-swiftshader`), and the
  tests use that.
- Use one viewer per page. Several instances work, but they share the single `#wlFilter` SVG filter that
  `viewer.js` uses for the live window/level preview.
- Prompts are display-only. `viewer.js` doesn't draw scribbles, boxes or points itself. They
  arrive as the `prompts` label layer from the backend.
- Tool and layout keyboard shortcuts belong to the host app, because `tool` and `layout` are props. The
  wrapper handles slice stepping and view reset only.
- **The nnInteractive model weights are not part of this package.** The official weights are
  licensed **CC BY-NC-SA 4.0** (non-commercial) and are downloaded separately by nnInteractive /
  nnInteractive Agent. This package doesn't do inference, run an agent or provide a chat UI. Those
  belong in the consuming app (for example a chat with reflex-assistant-ui and a backend built on
  `nninteractive_agent`).

## Development

```bash
uv sync
uv run pytest -m "not browser"                 # unit tests
uv run playwright install chromium
uv run pytest -m browser                       # starts the demo in production mode on a free port
NNI_DEMO_URL=http://localhost:3000 uv run pytest -m browser   # or test a running demo
uv run pre-commit run --all-files
uv build                                       # wheel + sdist, with generated .pyi stubs
```

Versions come from git tags (`v0.1.0` builds 0.1.0).

### Releasing

Releases are published from GitHub Actions with PyPI
[trusted publishing](https://docs.pypi.org/trusted-publishers/) (no API token):

1. Merge to `main` and wait for CI to pass.
2. Tag a commit of `main` and push the tag:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

   `.github/workflows/publish.yml` checks that the commit is on `main`, runs the tests,
   builds the sdist and wheel (with stubs), checks that the version equals the tag and that
   the README renders, publishes to PyPI (environment `pypi`) and creates a GitHub release
   with the distributions.
3. To try the pipeline first, run the *Publish* workflow by hand on `main`: it publishes a
   development version to TestPyPI (environment `testpypi`).

A version can be published once; bump the tag for every release.

## Credits and license

- **nnInteractive Agent**, Division of Medical Image Computing, German Cancer Research Center
  (DKFZ), Apache-2.0. `viewer.js`, `render3d.js`, `coords.js`, the file formats and the
  fake-session idea of the demo come from <https://github.com/MIC-DKFZ/nninteractive-agent>.
  Please cite:
  - Isensee, F., Ulrich Harsy, C., Rokuss, M., Hamm, B., Vollmuth, P., & Maier-Hein, K. H.
    (2026). *nnInteractive Agent: General-Purpose AI Agents as 3D Image Annotators.*
    <https://arxiv.org/abs/2610.31080>
  - Isensee, F., Rokuss, M., Krämer, L., et al. (2025). *nnInteractive: Redefining 3D Promptable
    Segmentation.* <https://arxiv.org/abs/2503.08373>
- **NiiVue** 0.69.0, BSD-2-Clause, <https://github.com/niivue/niivue>.

This package is licensed under Apache-2.0 (see [LICENSE](https://github.com/ecrespo/reflex-nninteractive-viewer/blob/main/LICENSE) and [NOTICE](https://github.com/ecrespo/reflex-nninteractive-viewer/blob/main/NOTICE)).
The vendored files keep their own licenses, which ship with the package.
