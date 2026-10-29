"""Exercise ``nni_viewer`` locally: a synthetic CT phantom, no GPU, no LLM, no agent.

Load the volume, toggle and focus objects, add one while the viewer is open (label patch
+ new surface), switch preset / window / layout / overlay / tool, and click a voxel to
see the coordinates arrive in Python state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import reflex as rx

from reflex_nninteractive_viewer import CT_WINDOWS, LAYOUTS, OVERLAY_MODES, TOOLS, nni_viewer
from reflex_nninteractive_viewer.server import (
    ViewerStore,
    bundle_patches,
    create_api,
    full_layer_patch,
    volume_meta,
)

from . import synthetic

KEY = "phantom"
# Inside the demo (gitignored), regenerated on every start: nothing pre-existing is trusted.
ROOT = Path(__file__).resolve().parent.parent / ".data"
STORE = ViewerStore(ROOT)
META = volume_meta(synthetic.write_volumes(STORE.volume_dir(KEY)), KEY)
INITIAL_IDS = [1, 2]
MESH_VERSIONS = {
    s.id: STORE.set_mesh(f"o{s.id}", synthetic.sphere_mesh(s)) for s in synthetic.SPHERES
}


def _sync_labels(ids: list[int]) -> None:
    """Serve the label layers for ``ids`` and stream the new objects layer to open viewers."""
    STORE.set_patch_bundle(KEY, bundle_patches(synthetic.layer_patches(ids)))
    STORE.publish(KEY, full_layer_patch("objects", synthetic.labels(ids)))


_sync_labels(INITIAL_IDS)


def _object(s: synthetic.Sphere, hidden: bool = False) -> dict[str, Any]:
    return {"id": s.id, "name": s.name, "color": s.color, "hidden": hidden, "current": False}


def _mesh(s: synthetic.Sphere) -> dict[str, Any]:
    v = MESH_VERSIONS[s.id]
    return {
        "key": f"o{s.id}",
        "url": f"api/mesh/o{s.id}?v={v}",
        "version": v,
        "color": s.color,
        "name": s.name,
        "opacity": 1.0,
    }


class DemoState(rx.State):
    """What the demo page controls and what the viewer reports back."""

    loaded: bool = False
    ids: list[int] = []
    hidden: list[int] = []
    preset: str = "Bones + skin"
    presets: list[str] = []
    window: int = 0
    windows: list[str] = list(CT_WINDOWS)
    window_text: str = ""
    layout: str = "grid"
    overlay_mode: str = "fill"
    tool: str = "navigate"
    surface_opacity: float = 1.0
    streaming: bool = True
    focus_object: int | None = None
    ready: bool = False
    location: dict[str, Any] = {}
    slice_voxel: list[int] = []
    error: str = ""

    @rx.var
    def volume_meta(self) -> dict[str, Any] | None:
        return META if self.loaded else None

    @rx.var
    def objects(self) -> list[dict[str, Any]]:
        return [_object(s, s.id in self.hidden) for s in synthetic.SPHERES if s.id in self.ids]

    @rx.var
    def meshes(self) -> list[dict[str, Any]]:
        return [_mesh(s) for s in synthetic.SPHERES if s.id in self.ids]

    @rx.var
    def patch_url(self) -> str | None:
        return f"api/patch/{KEY}" if self.loaded else None

    @rx.var
    def patch_ws_url(self) -> str | None:
        return f"api/patch_ws/{KEY}" if self.loaded and self.streaming else None

    @rx.var
    def window_name(self) -> str:
        return self.windows[self.window] if 0 <= self.window < len(self.windows) else ""

    @rx.var
    def can_add(self) -> bool:
        return self.loaded and len(self.ids) < len(synthetic.SPHERES)

    # ------------------------------------------------------------ controls
    @rx.event
    def load_volume(self):
        self.ids = list(INITIAL_IDS)
        self.hidden = []
        _sync_labels(self.ids)
        self.ready = False
        self.error = ""
        self.loaded = True

    @rx.event
    def toggle_object(self, object_id: int):
        if object_id in self.hidden:
            self.hidden = [i for i in self.hidden if i != object_id]
        else:
            self.hidden = [*self.hidden, object_id]

    @rx.event
    def add_object(self):
        nxt = next((s.id for s in synthetic.SPHERES if s.id not in self.ids), None)
        if nxt is None:
            return
        self.ids = [*self.ids, nxt]
        _sync_labels(self.ids)

    @rx.event
    def remove_last_object(self):
        if len(self.ids) > 1:
            self.ids = self.ids[:-1]
            _sync_labels(self.ids)

    @rx.event
    def focus(self, object_id: int):
        self.focus_object = object_id

    @rx.event
    def set_preset(self, value: str):
        self.preset = value

    @rx.event
    def set_window_name(self, name: str):
        if name in self.windows:
            self.window = self.windows.index(name)

    @rx.event
    def set_layout(self, value: str):
        self.layout = value

    @rx.event
    def set_overlay_mode(self, value: str):
        self.overlay_mode = value

    @rx.event
    def set_tool(self, value: str):
        self.tool = value

    @rx.event
    def set_surface_opacity(self, value: list[float]):
        self.surface_opacity = round(value[0], 2)

    @rx.event
    def set_streaming(self, value: bool):
        self.streaming = value

    # -------------------------------------------------------- viewer events
    @rx.event
    def on_ready(self, info: dict[str, Any]):
        self.ready = True
        self.windows = info.get("windows") or list(CT_WINDOWS)
        self.presets = info.get("presets") or []

    @rx.event
    def on_location(self, voxel: list[int], mm: list[float], value: float):
        self.location = {
            "voxel": voxel,
            "mm": [round(float(c), 1) for c in mm],
            "value": value,
        }

    @rx.event
    def on_slice_change(self, voxel: list[int]):
        self.slice_voxel = voxel
        self.focus_object = None  # so focusing the same object again moves the crosshair

    @rx.event
    def on_error(self, message: str):
        self.error = message

    @rx.event
    def on_window_change(self, lo: float, hi: float, preset: int | None):
        if preset is not None:
            self.window = preset
        self.window_text = f"W {round(hi - lo)} / L {round((hi + lo) / 2)}"


# --------------------------------------------------------------------------- page
def _object_row(obj: rx.Var[dict[str, Any]]) -> rx.Component:
    return rx.hstack(
        rx.box(width="12px", height="12px", border_radius="3px", background=obj["color"]),
        rx.text(obj["name"], size="2", flex="1"),
        rx.switch(
            checked=~obj["hidden"].to(bool),
            on_change=lambda _: DemoState.toggle_object(obj["id"].to(int)),
            size="1",
        ),
        rx.icon_button(
            rx.icon("crosshair", size=14),
            size="1",
            variant="soft",
            on_click=DemoState.focus(obj["id"].to(int)),
        ),
        width="100%",
        align="center",
    )


def _labeled(label: str, control: rx.Component) -> rx.Component:
    return rx.vstack(
        rx.text(label, size="1", color_scheme="gray"), control, spacing="1", width="100%"
    )


def _controls() -> rx.Component:
    return rx.vstack(
        rx.heading("nni_viewer demo", size="4"),
        rx.button(
            rx.cond(DemoState.loaded, "Reload volume", "Load volume"),
            on_click=DemoState.load_volume,
            width="100%",
        ),
        rx.divider(),
        rx.text("Objects", weight="bold", size="2"),
        rx.foreach(DemoState.objects, _object_row),
        rx.hstack(
            rx.button(
                "Add object", size="1", on_click=DemoState.add_object, disabled=~DemoState.can_add
            ),
            rx.button(
                "Remove last", size="1", variant="soft", on_click=DemoState.remove_last_object
            ),
        ),
        rx.hstack(
            rx.switch(checked=DemoState.streaming, on_change=DemoState.set_streaming, size="1"),
            rx.text("Stream label patches (WebSocket)", size="1"),
            align="center",
        ),
        rx.divider(),
        _labeled(
            "3D preset",
            rx.select(
                DemoState.presets,
                value=DemoState.preset,
                on_change=DemoState.set_preset,
                width="100%",
            ),
        ),
        _labeled(
            "Window",
            rx.select(
                DemoState.windows,
                value=DemoState.window_name,
                on_change=DemoState.set_window_name,
                width="100%",
            ),
        ),
        rx.text(DemoState.window_text, size="1", color_scheme="gray"),
        _labeled(
            "Layout",
            rx.select(
                list(LAYOUTS), value=DemoState.layout, on_change=DemoState.set_layout, width="100%"
            ),
        ),
        _labeled(
            "Objects on slices",
            rx.select(
                list(OVERLAY_MODES),
                value=DemoState.overlay_mode,
                on_change=DemoState.set_overlay_mode,
                width="100%",
            ),
        ),
        _labeled(
            "Left-button tool",
            rx.select(
                list(TOOLS), value=DemoState.tool, on_change=DemoState.set_tool, width="100%"
            ),
        ),
        _labeled(
            "Surface opacity",
            rx.slider(
                default_value=[1.0],
                min=0,
                max=1,
                step=0.05,
                on_value_commit=DemoState.set_surface_opacity,
                width="100%",
            ),
        ),
        rx.divider(),
        rx.text("Mention location (click a voxel)", weight="bold", size="2"),
        rx.cond(
            DemoState.location,
            rx.text(
                DemoState.location.to_string(),
                font_family="monospace",
                size="1",
                width="100%",
                word_break="break-all",
                id="location",
            ),
            rx.text("No click yet", size="1", color_scheme="gray"),
        ),
        rx.text(
            "Crosshair: ",
            DemoState.slice_voxel.to_string(),
            size="1",
            color_scheme="gray",
            id="slice",
        ),
        rx.cond(
            DemoState.error != "",
            rx.callout(DemoState.error, color_scheme="red", size="1", width="100%"),
        ),
        width="300px",
        min_width="300px",
        padding="16px",
        spacing="3",
        height="100vh",
        overflow_y="auto",
    )


def index() -> rx.Component:
    return rx.hstack(
        rx.box(
            nni_viewer(
                volume_meta=DemoState.volume_meta,
                objects=DemoState.objects,
                meshes=DemoState.meshes,
                patch_url=DemoState.patch_url,
                patch_ws_url=DemoState.patch_ws_url,
                preset=DemoState.preset,
                window=DemoState.window,
                surface_opacity=DemoState.surface_opacity,
                overlay_mode=DemoState.overlay_mode,
                layout=DemoState.layout,
                tool=DemoState.tool,
                focus_object=DemoState.focus_object,
                on_ready=DemoState.on_ready,
                on_location=DemoState.on_location,
                on_slice_change=DemoState.on_slice_change,
                on_layout_request=DemoState.set_layout,
                on_window_change=DemoState.on_window_change,
                on_error=DemoState.on_error,
                id="viewer",
            ),
            flex="1",
            height="100vh",
            min_width="0",
        ),
        _controls(),
        spacing="0",
        width="100vw",
        height="100vh",
        background="var(--gray-1)",
    )


app = rx.App(api_transformer=create_api(STORE))
app.add_page(index, title="nni_viewer demo")
