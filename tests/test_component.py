"""The component: what it imports, how props render, prop validation and event specs."""

import hashlib
import re
from pathlib import Path

import pytest
import reflex as rx

from reflex_nninteractive_viewer import NNInteractiveViewer, nni_viewer

ASSETS = Path(__file__).resolve().parents[1] / "src" / "reflex_nninteractive_viewer" / "assets"
META = {
    "key": "k",
    "url": "api/volume/k/image.nii.gz",
    "url3d": "api/volume/k/image3d.nii.gz",
    "shape": [4, 5, 6],
    "range": [0.0, 1.0],
    "auto_window": [0.0, 1.0],
    "is_ct": False,
    "is_2d": False,
}


def rendered(component) -> str:
    return str(component).replace(" ", "")


# ----------------------------------------------------------------------- imports
def test_library_is_the_shared_wrapper_asset():
    component = nni_viewer()
    lib = component.library
    assert lib is not None
    assert lib.startswith("$/public/external/reflex_nninteractive_viewer/")
    assert lib.endswith("/assets/nni_viewer.jsx")
    assert component.tag == "NniViewer"
    assert not component.is_default


def test_is_imported_dynamically_on_the_client_only():
    component = nni_viewer()
    assert isinstance(component, rx.NoSSRComponent)
    code = component._get_dynamic_imports()
    assert "ClientSide(" in code
    assert f"import('{component.library}')" in code
    assert ".then((mod) => mod.NniViewer)" in code


def test_wrapper_and_vendored_modules_are_linked_next_to_each_other():
    # Importing the component links them into ./assets/external (the compiling app).
    base = Path.cwd() / "assets" / "external" / "reflex_nninteractive_viewer"
    base = base / "nninteractive_viewer" / "assets"
    for name in ("nni_viewer.jsx", "viewer.js", "render3d.js", "coords.js"):
        assert (base / name).resolve() == (ASSETS / name).resolve()
    assert (base / "vendor" / "niivue-0.69.0.esm.js").resolve().is_file()


def test_wrapper_imports_only_what_is_shipped():
    jsx = (ASSETS / "nni_viewer.jsx").read_text()
    relative = set(re.findall(r'from "(\./[^"]+)"', jsx))
    assert relative == {"./viewer.js"}
    viewer = (ASSETS / "viewer.js").read_text()
    for imp in re.findall(r"from '(\./[^']+)'", viewer):
        assert (ASSETS / imp).is_file(), imp


@pytest.mark.parametrize("name", ["render3d.js", "coords.js", "vendor/niivue-0.69.0.esm.js"])
def test_unpatched_vendored_files_match_the_recorded_upstream_checksum(name):
    table = (ASSETS / "PATCHES.md").read_text()
    row = next(line for line in table.splitlines() if line.startswith(f"| `{name}`"))
    expected = re.findall(r"`([0-9a-f]{64})`", row)[0]
    assert hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() == expected


def test_viewer_patch_is_the_documented_one():
    table = (ASSETS / "PATCHES.md").read_text()
    after = re.search(r"SHA-256 after the patch:\s*`([0-9a-f]{64})`", table)
    assert after is not None
    assert hashlib.sha256((ASSETS / "viewer.js").read_bytes()).hexdigest() == after.group(1)


def test_licenses_and_notice_are_shipped():
    assert (ASSETS / "LICENSE-nninteractive-agent").is_file()
    assert (ASSETS / "vendor" / "LICENSE-niivue").is_file()
    notice = (Path(__file__).resolve().parents[1] / "NOTICE").read_text()
    assert "MIC-DKFZ" in notice and "NiiVue" in notice and "CC BY-NC-SA" in notice


# ------------------------------------------------------------------------- props
def test_props_are_sent_to_react_in_camel_case():
    out = rendered(
        nni_viewer(
            volume_meta=META,
            objects=[{"id": 1, "name": "a", "color": "#ff0000", "hidden": False}],
            meshes=[{"key": "o1", "url": "api/mesh/o1", "version": 2, "color": "#ff0000"}],
            patch_url="api/patch/k",
            patch_ws_url="api/patch_ws/k",
            api_base="http://localhost:8000/",
            preset="MIP",
            window=1,
            surface_opacity=0.5,
            overlay_mode="outline",
            layout="axial",
            tool="zoom",
            focus=[1, 2, 3],
            focus_object=1,
            keyboard=False,
        )
    )
    for prop in (
        "volumeMeta:",
        "objects:",
        "meshes:",
        'patchUrl:"api/patch/k"',
        'patchWsUrl:"api/patch_ws/k"',
        'apiBase:"http://localhost:8000/"',
        'preset:"MIP"',
        "window:1",
        "surfaceOpacity:0.5",
        'overlayMode:"outline"',
        'layout:"axial"',
        'tool:"zoom"',
        "focus:[1,2,3]",
        "focusObject:1",
        "keyboard:false",
    ):
        assert prop in out, prop


def test_fills_its_parent_by_default():
    out = rendered(nni_viewer())
    assert '["width"]:"100%"' in out and '["height"]:"100%"' in out


def test_state_vars_are_accepted_without_validation():
    class S(rx.State):
        layout: str = "grid"
        tool: str = "navigate"
        opacity: float = 1.0
        window: int = 0
        meta: dict = {}

    out = rendered(
        nni_viewer(
            layout=S.layout,
            tool=S.tool,
            surface_opacity=S.opacity,
            window=S.window,
            volume_meta=S.meta,
        )
    )
    assert "layout:" in out and "surfaceOpacity:" in out


@pytest.mark.parametrize(
    ("props", "message"),
    [
        ({"overlay_mode": "solid"}, "overlay_mode"),
        ({"layout": "quad"}, "layout"),
        ({"tool": "brush"}, "tool"),
        ({"surface_opacity": 1.5}, "surface_opacity"),
        ({"surface_opacity": -0.1}, "surface_opacity"),
        ({"window": -1}, "window"),
        ({"window": True}, "window"),
        ({"window": 1.5}, "window"),
        ({"focus": [1, 2]}, "focus"),
        ({"volume_meta": {"url": "x"}}, "volume_meta"),
        ({"objects": [{"id": 1}]}, "objects"),
        ({"meshes": [{"url": "x"}]}, "meshes"),
    ],
)
def test_invalid_literal_props_are_rejected(props, message):
    with pytest.raises(ValueError, match=message):
        nni_viewer(**props)


def test_children_are_rejected():
    with pytest.raises(ValueError, match="children"):
        nni_viewer(rx.text("x"))


def test_create_returns_the_component():
    assert isinstance(nni_viewer(), NNInteractiveViewer)


# ------------------------------------------------------------------------ events
EVENTS = {
    "on_location": 3,
    "on_ready": 1,
    "on_error": 1,
    "on_slice_change": 1,
    "on_layout_request": 1,
    "on_window_change": 3,
}


@pytest.mark.parametrize(("name", "arity"), EVENTS.items())
def test_event_specs_pass_the_documented_arguments(name, arity):
    spec = NNInteractiveViewer.get_event_triggers()[name]
    args = [rx.Var(f"a{i}") for i in range(arity)]
    assert len(spec(*args)) == arity  # pyright: ignore[reportCallIssue]


def test_handlers_receive_the_event_arguments():
    class ViewerState(rx.State):
        voxel: list[int] = []
        mm: list[float] = []
        value: float = 0
        info: dict = {}
        error: str = ""
        slice: list[int] = []
        layout: str = "grid"
        lo: float = 0
        hi: float = 0

        @rx.event
        def located(self, voxel: list[int], mm: list[float], value: float):
            self.voxel, self.mm, self.value = voxel, mm, value

        @rx.event
        def ready(self, info: dict):
            self.info = info

        @rx.event
        def failed(self, message: str):
            self.error = message

        @rx.event
        def moved(self, voxel: list[int]):
            self.slice = voxel

        @rx.event
        def relayout(self, mode: str):
            self.layout = mode

        @rx.event
        def windowed(self, lo: float, hi: float, preset: int | None):
            self.lo, self.hi = lo, hi

    component = nni_viewer(
        on_location=ViewerState.located,
        on_ready=ViewerState.ready,
        on_error=ViewerState.failed,
        on_slice_change=ViewerState.moved,
        on_layout_request=ViewerState.relayout,
        on_window_change=ViewerState.windowed,
    )
    out = str(component)
    for trigger in (
        "onLocation",
        "onReady",
        "onError",
        "onSliceChange",
        "onLayoutRequest",
        "onWindowChange",
    ):
        assert trigger in out, trigger
    assert set(EVENTS) <= set(component.get_event_triggers())
    # The three arguments of on_location reach the handler in order.
    assert re.search(r"onLocation:\(\(_voxel,_mm,_value\)", out.replace(" ", ""))


def test_handler_with_too_many_arguments_is_rejected():
    class Bad(rx.State):
        @rx.event
        def too_many(self, a: list[int], b: list[float], c: float, d: int):
            pass

    with pytest.raises(Exception):  # noqa: B017 - Reflex raises its own EventHandlerArgTypeMismatch
        nni_viewer(on_location=Bad.too_many)  # pyright: ignore[reportArgumentType]
