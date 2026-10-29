"""The ``nni_viewer`` component: the nnInteractive agent's slice + 3D viewer for Reflex.

The browser side is the upstream ``viewer.js`` (NiiVue slice views and the ``render3d.js``
WebGL2 renderer), wrapped by ``assets/nni_viewer.jsx``. Props carry URLs and metadata
only; the viewer downloads volumes, meshes and label patches itself, so no image data
passes through Reflex state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import reflex as rx
from reflex import constants
from reflex.components.component import NoSSRComponent
from reflex.environment import EnvironmentVariables
from reflex.vars import Var

# The wrapper and every module it imports must be linked into the app's assets, keeping
# the relative layout the ES module imports rely on (./viewer.js, ./vendor/...).
_VENDORED = (
    "viewer.js",
    "render3d.js",
    "coords.js",
    "vendor/niivue-0.69.0.esm.js",
)

OverlayMode = Literal["fill", "outline", "off"]
Layout = Literal["grid", "axial", "coronal", "sagittal", "3d", "single"]
Tool = Literal["navigate", "window", "scroll", "pan", "zoom"]

OVERLAY_MODES: tuple[str, ...] = ("fill", "outline", "off")
LAYOUTS: tuple[str, ...] = ("grid", "axial", "coronal", "sagittal", "3d", "single")
TOOLS: tuple[str, ...] = ("navigate", "window", "scroll", "pan", "zoom")
CT_PRESETS: tuple[str, ...] = ("Bones", "Bones + skin", "Skin", "Off")
"""3D view presets offered for CT images (``volume_meta["is_ct"]``)."""
NON_CT_PRESETS: tuple[str, ...] = ("Translucent", "MIP", "Off")
"""3D view presets offered for other images."""
CT_WINDOWS: tuple[str, ...] = ("Soft tissue", "Liver", "Lung", "Bone", "Brain", "Full range")
"""Window presets (``window`` is an index into this) for CT images."""
NON_CT_WINDOWS: tuple[str, ...] = ("Auto", "Full range")
"""Window presets for other images."""

_REQUIRED_META_KEYS = ("url", "shape", "range", "auto_window")


def _link_assets() -> str:
    """Link the JS modules into the compiling app; return the wrapper's import path."""
    if not EnvironmentVariables.REFLEX_BACKEND_ONLY.get():
        # rx.asset creates the module's folder but not subfolders such as assets/vendor/.
        external = Path.cwd() / constants.Dirs.APP_ASSETS / constants.Dirs.EXTERNAL_APP_ASSETS
        package = external / __name__.replace(".", "/") / "assets"
        for sub in {Path(p).parent for p in _VENDORED}:
            (package / sub).mkdir(parents=True, exist_ok=True)
    for path in _VENDORED:
        rx.asset(f"assets/{path}", shared=True, _stack_level=2)
    return rx.asset("assets/nni_viewer.jsx", shared=True, _stack_level=2).importable_path


def _location_spec(
    voxel: Var[list[int]], mm: Var[list[float]], value: Var[float]
) -> tuple[Var[list[int]], Var[list[float]], Var[float]]:
    """``on_location``: workspace voxel [axis0, axis1, axis2], RAS mm, image value."""
    return voxel, mm, value


def _slice_spec(voxel: Var[list[int]]) -> tuple[Var[list[int]]]:
    """``on_slice_change``: the workspace voxel at the crosshair."""
    return (voxel,)


def _ready_spec(info: Var[dict[str, Any]]) -> tuple[Var[dict[str, Any]]]:
    """``on_ready``: ``{"windows": [...], "presets": [...], "layout": str, "preset": str}``."""
    return (info,)


def _message_spec(message: Var[str]) -> tuple[Var[str]]:
    """``on_error``: a human-readable message."""
    return (message,)


def _layout_spec(mode: Var[str]) -> tuple[Var[str]]:
    """``on_layout_request``: the layout a tile label click or double-click asks for."""
    return (mode,)


def _window_spec(
    lo: Var[float], hi: Var[float], preset: Var[int | None]
) -> tuple[Var[float], Var[float], Var[int | None]]:
    """``on_window_change``: window bounds and the preset index (None after a drag)."""
    return lo, hi, preset


def _check_literal(name: str, value: Any, allowed: tuple[str, ...]) -> None:
    if value is None or isinstance(value, rx.Var):
        return
    if value not in allowed:
        msg = f"{name} must be one of {', '.join(map(repr, allowed))}, got {value!r}"
        raise ValueError(msg)


def _validate(props: dict[str, Any]) -> None:
    """Reject literal prop values the viewer would silently ignore."""
    _check_literal("overlay_mode", props.get("overlay_mode"), OVERLAY_MODES)
    _check_literal("layout", props.get("layout"), LAYOUTS)
    _check_literal("tool", props.get("tool"), TOOLS)

    opacity = props.get("surface_opacity")
    if opacity is not None and not isinstance(opacity, rx.Var):
        if not 0 <= float(opacity) <= 1:
            msg = f"surface_opacity must be between 0 and 1, got {opacity!r}"
            raise ValueError(msg)

    window = props.get("window")
    if window is not None and not isinstance(window, rx.Var):
        if isinstance(window, bool) or not isinstance(window, int) or window < 0:
            msg = f"window must be a non-negative preset index, got {window!r}"
            raise ValueError(msg)

    focus = props.get("focus")
    if focus is not None and not isinstance(focus, rx.Var):
        if len(focus) != 3:
            msg = f"focus must be a workspace voxel [axis0, axis1, axis2], got {focus!r}"
            raise ValueError(msg)

    meta = props.get("volume_meta")
    if isinstance(meta, dict):
        missing = [k for k in _REQUIRED_META_KEYS if k not in meta]
        if missing:
            msg = f"volume_meta is missing {', '.join(missing)}"
            raise ValueError(msg)

    for name, required in (("objects", ("id", "color")), ("meshes", ("key", "url"))):
        items = props.get(name)
        if isinstance(items, list | tuple):
            for item in items:
                if isinstance(item, dict) and (lack := [k for k in required if k not in item]):
                    msg = f"each entry of {name} needs {', '.join(required)}; {item!r} lacks "
                    raise ValueError(msg + ", ".join(lack))


class NNInteractiveViewer(NoSSRComponent):
    """Three slice views and a 3D view of a medical image, with label overlays and surfaces.

    Wraps ``viewer.js`` from MIC-DKFZ/nninteractive-agent. Needs WebGL2.
    """

    library = _link_assets()

    tag = "NniViewer"

    # The image: the ``image`` message of nninteractive_agent's web session, i.e. the dict
    # ``layers.write_volumes`` returns plus ``key``, ``url`` (image.nii.gz) and ``url3d``
    # (image3d.nii.gz). ``zeros_uint8.nii.gz``/``zeros_uint16.nii.gz`` must be served next
    # to ``url``. A new ``key`` (or ``n``/``url``) loads a new image.
    volume_meta: rx.Var[dict[str, Any] | None]

    # Objects: ``{"id": int, "name": str, "color": "#rrggbb", "hidden": bool, "current": bool}``.
    objects: rx.Var[list[dict[str, Any]]]

    # Surfaces: ``{"key": str, "url": str, "version": int, "color": "#rrggbb", "name": str,
    # "opacity": float}``. Diffed by key/version/url/color: unchanged meshes are not refetched.
    # Keys ``o<id>`` follow the visibility of object ``id``.
    meshes: rx.Var[list[dict[str, Any]]]

    # URL of a patch bundle with the label layers (objects/current/prompts), refetched
    # whenever ``objects`` changes. See ``server.patch_bundle``.
    patch_url: rx.Var[str | None]

    # WebSocket URL whose binary messages are single label patches (live updates while the
    # agent works). Can be combined with ``patch_url``.
    patch_ws_url: rx.Var[str | None]

    # Base URL for relative URLs in the other props. Default: the Reflex backend.
    api_base: rx.Var[str | None]

    # 3D view preset name (``CT_PRESETS`` or ``NON_CT_PRESETS``).
    preset: rx.Var[str | None]

    # Window preset index (``CT_WINDOWS`` or ``NON_CT_WINDOWS``).
    window: rx.Var[int | None]

    # Opacity of all surfaces in the 3D view, 0..1.
    surface_opacity: rx.Var[float | None]

    # How objects are drawn on the slices: "fill", "outline" or "off".
    overlay_mode: rx.Var[str | None]

    # "grid", "axial", "coronal", "sagittal", "3d" (enlarged view + the others), or "single".
    layout: rx.Var[str | None]

    # Left mouse button tool: "navigate", "window", "scroll", "pan" or "zoom".
    tool: rx.Var[str | None]

    # Move the crosshair to this workspace voxel [axis0, axis1, axis2].
    focus: rx.Var[list[int] | None]

    # Move the crosshair to this object (its surface centre, else its voxels' centre).
    focus_object: rx.Var[int | None]

    # Arrow keys / Page Up / Page Down step slices and R resets the view (when focused).
    keyboard: rx.Var[bool]

    # A click without dragging in the slice views ("Mention location").
    on_location: rx.EventHandler[_location_spec]

    # The image is loaded and displayed.
    on_ready: rx.EventHandler[_ready_spec]

    # The viewer could not start (no WebGL2) or a download failed.
    on_error: rx.EventHandler[_message_spec]

    # The crosshair moved (scrolling, keys, clicks); debounced.
    on_slice_change: rx.EventHandler[_slice_spec]

    # A tile label was clicked or a view double-clicked; set ``layout`` to honour it.
    on_layout_request: rx.EventHandler[_layout_spec]

    # The window changed (preset applied or a window/level drag ended).
    on_window_change: rx.EventHandler[_window_spec]

    @classmethod
    def create(cls, *children, **props) -> NNInteractiveViewer:
        """Create the viewer.

        Args:
            *children: Not supported; the viewer renders its own content.
            **props: The component props.

        Returns:
            The component.

        Raises:
            ValueError: For children or for invalid literal prop values.

        """
        if children:
            msg = "nni_viewer does not take children"
            raise ValueError(msg)
        _validate(props)
        props.setdefault("width", "100%")
        props.setdefault("height", "100%")
        return super().create(**props)  # type: ignore[return-value]


nni_viewer = NNInteractiveViewer.create
