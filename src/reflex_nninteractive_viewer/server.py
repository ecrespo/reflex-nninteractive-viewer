"""FastAPI routes that serve what ``nni_viewer`` fetches, ready for ``rx.App(api_transformer=...)``.

The routes serve files and blobs in the formats nninteractive_agent's web demo writes
(``nninteractive_agent.web.layers``), so its output can be used unchanged::

    store = ViewerStore("/tmp/nni_viewer")
    app = rx.App(api_transformer=create_api(store))

Routes (``prefix`` defaults to ``/api``):

- ``GET  {prefix}/volume/{key}/{name}``: ``image.nii.gz``, ``image3d.nii.gz``,
  ``zeros_uint8.nii.gz`` or ``zeros_uint16.nii.gz`` from ``store.volume_dir(key)``.
- ``GET  {prefix}/mesh/{key}``: a mesh blob (``crop_mesh`` format), 204 when there is none.
- ``GET  {prefix}/img/{name}``: a PNG from ``store.images_dir`` (the agent's view images).
- ``GET  {prefix}/patch/{key}``: the patch bundle of image ``key`` (``patch_url``), 204 if none.
- ``WS   {prefix}/patch_ws/{key}``: label patches of image ``key`` as they are published
  (``patch_ws_url``).

Everything that needs ``nninteractive_agent`` imports it inside the function, so this
module (and the component) installs without it. The store keeps meshes, bundles and
WebSocket subscribers in memory: run the Reflex backend as a single process.

The routes are public unless you pass ``authorize`` (see :func:`create_router`). Medical
images are sensitive: check the user's session there in anything but a local demo.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import struct
import threading
import zlib
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from starlette.requests import HTTPConnection

VOLUME_FILES = ("image.nii.gz", "image3d.nii.gz", "zeros_uint8.nii.gz", "zeros_uint16.nii.gz")
"""The files of one image that the viewer downloads (written by ``layers.write_volumes``)."""

_KEY = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_PNG = re.compile(r"[A-Za-z0-9_-]{1,128}\.png")


def _valid_key(key: str) -> bool:
    return bool(_KEY.fullmatch(key)) and not key.startswith(".")


def _check_key(key: str) -> str:
    """Reject keys that could escape the store's directories (raises ValueError)."""
    if not _valid_key(key):
        msg = f"invalid key {key!r}: use letters, digits, '_', '-' and '.' (not first)"
        raise ValueError(msg)
    return key


def _route_key(key: str) -> str:
    if not _valid_key(key):
        raise HTTPException(404)
    return key


# ------------------------------------------------------------------------- formats
def encode_patch(header: dict[str, Any], raw: bytes) -> bytes:
    """One label patch as ``viewer.applyPatch`` reads it (same as ``layers.encode_patch``).

    ``uint32 LE`` header length, the JSON header, then the zlib-compressed C-order bytes of
    the crop. The header holds ``layer`` ("objects", "current" or "prompts"), ``box``
    (half-open ``[[lo, hi]] * 3`` in workspace axes), ``dtype`` ("uint8"/"uint16"),
    ``shape`` (the full workspace shape) and ``full`` (whether the crop is the whole layer).
    """
    head = json.dumps(header).encode()
    return struct.pack("<I", len(head)) + head + (zlib.compress(raw, 1) if raw else b"")


def full_layer_patch(layer: str, data: Any) -> bytes:
    """Encode a whole layer (array-like with ``shape``, ``dtype``, ``tobytes()``) as a patch."""
    shape = [int(s) for s in data.shape]
    header = {
        "layer": layer,
        "box": [[0, s] for s in shape],
        "dtype": str(data.dtype),
        "shape": shape,
        "full": True,
        "seq": 0,
    }
    return encode_patch(header, data.tobytes(order="C"))


def bundle_patches(patches: Iterable[bytes]) -> bytes:
    """Concatenate patches into a bundle: each one prefixed with its ``uint32 LE`` length."""
    return b"".join(struct.pack("<I", len(p)) + p for p in patches)


def split_bundle(blob: bytes) -> list[bytes]:
    """Inverse of :func:`bundle_patches`."""
    out, off = [], 0
    while off + 4 <= len(blob):
        (n,) = struct.unpack_from("<I", blob, off)
        if off + 4 + n > len(blob):
            msg = "truncated patch bundle"
            raise ValueError(msg)
        out.append(blob[off + 4 : off + 4 + n])
        off += 4 + n
    return out


def encode_mesh(vertices: Any, triangles: Any) -> bytes:
    """Encode a mesh blob as ``crop_mesh`` writes it and ``viewer.setMesh`` reads it.

    ``uint32 LE`` vertex count, ``uint32 LE`` triangle count, ``float32 LE`` vertices
    (x, y, z in RAS mm), ``uint32 LE`` vertex indices (counter-clockwise seen from outside).
    Accepts numpy arrays (``(n, 3)``) or nested sequences.
    """
    if hasattr(vertices, "astype"):
        v = vertices.astype("<f4").tobytes()
        t = triangles.astype("<u4").tobytes()
        nv, nt = len(vertices), len(triangles)
    else:
        verts = [float(c) for p in vertices for c in p]
        tris = [int(i) for f in triangles for i in f]
        nv, nt = len(verts) // 3, len(tris) // 3
        v = struct.pack(f"<{len(verts)}f", *verts)
        t = struct.pack(f"<{len(tris)}I", *tris)
    return struct.pack("<II", nv, nt) + v + t


# --------------------------------------------------------------------------- store
class ViewerStore:
    """What the routes serve: image directories, meshes, patch bundles and live patches.

    Args:
        root: Directory holding one sub-directory per image key (the files of
            ``VOLUME_FILES``), unless :meth:`add_volume_dir` registers another place.
        images_dir: Directory of the PNGs served under ``/img`` (default ``root/images``).

    """

    def __init__(self, root: str | Path, images_dir: str | Path | None = None):  # noqa: D107
        self.root = Path(root)
        self.images_dir = Path(images_dir) if images_dir else self.root / "images"
        self._dirs: dict[str, Path] = {}
        self._meshes: dict[str, tuple[int, bytes]] = {}
        self._bundles: dict[str, bytes] = {}
        self._subscribers: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
        self._lock = threading.Lock()

    # -- volumes
    def volume_dir(self, key: str) -> Path:
        """Directory of image ``key`` (raises ValueError for keys that are not plain names)."""
        _check_key(key)
        return self._dirs.get(key) or self.root / key

    def add_volume_dir(self, key: str, path: str | Path) -> None:
        """Serve image ``key`` from ``path`` instead of ``root/key``."""
        _check_key(key)
        self._dirs[key] = Path(path)

    # -- meshes
    def set_mesh(self, key: str, blob: bytes) -> int:
        """Store a mesh blob; returns its new version (put it in the ``meshes`` prop)."""
        _check_key(key)
        with self._lock:
            version = self._meshes.get(key, (0, b""))[0] + 1
            self._meshes[key] = (version, blob)
        return version

    def mesh(self, key: str) -> bytes | None:
        """Return the current blob of mesh ``key``, if any."""
        m = self._meshes.get(key)
        return m[1] if m else None

    def mesh_version(self, key: str) -> int:
        """Return the current version of mesh ``key`` (0 when there is none)."""
        return self._meshes.get(key, (0, b""))[0]

    def remove_mesh(self, key: str) -> None:
        """Forget mesh ``key``."""
        with self._lock:
            self._meshes.pop(key, None)

    # -- label layers
    def set_patch_bundle(self, key: str, blob: bytes) -> None:
        """Set the patch bundle served for image ``key`` (see :func:`bundle_patches`)."""
        _check_key(key)
        self._bundles[key] = blob

    def patch_bundle(self, key: str) -> bytes | None:
        """Return the patch bundle of image ``key``, if any."""
        return self._bundles.get(key)

    def publish(self, key: str, patch: bytes) -> None:
        """Send one patch to every WebSocket subscribed to image ``key`` (thread-safe).

        A subscriber that falls more than ``MAX_QUEUED_PATCHES`` behind is disconnected
        (its page reconnects and resynchronizes through ``patch_url``) instead of
        buffering without bound.
        """
        with self._lock:
            subs = list(self._subscribers.get(key, ()))
        for loop, queue in subs:
            loop.call_soon_threadsafe(_offer, queue, patch)

    def _subscribe(self, key: str) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue]:
        sub = (asyncio.get_running_loop(), asyncio.Queue(maxsize=MAX_QUEUED_PATCHES + 1))
        with self._lock:
            self._subscribers.setdefault(key, set()).add(sub)
        return sub

    def _unsubscribe(self, key: str, sub: tuple[asyncio.AbstractEventLoop, asyncio.Queue]) -> None:
        with self._lock:
            self._subscribers.get(key, set()).discard(sub)


MAX_QUEUED_PATCHES = 256
"""Patches a WebSocket subscriber may lag behind before it is disconnected."""
_OVERFLOW = object()


def _offer(queue: asyncio.Queue, patch: bytes) -> None:
    if queue.qsize() >= MAX_QUEUED_PATCHES:
        if not queue.full():
            queue.put_nowait(_OVERFLOW)
        return
    queue.put_nowait(patch)


# -------------------------------------------------------------------------- routes
Authorize = Callable[[HTTPConnection, str], bool | Awaitable[bool]]
"""``authorize(request_or_websocket, key) -> bool``: may this connection read ``key``?"""


def create_router(
    store: ViewerStore, prefix: str = "/api", authorize: Authorize | None = None
) -> APIRouter:
    """Build the viewer's routes on an ``APIRouter`` (to include in an existing FastAPI app).

    Args:
        store: What to serve.
        prefix: URL prefix of the routes.
        authorize: Called with the request (or WebSocket) and the image, mesh or PNG key
            before anything is served; return False to answer 404. Without it every
            route is public: use unguessable keys (``secrets.token_urlsafe``) at least,
            and an ``authorize`` that checks the user's session for anything but a demo.

    Returns:
        The router.

    """
    router = APIRouter(prefix=prefix)

    async def allowed(conn: HTTPConnection, key: str) -> bool:
        if authorize is None:
            return True
        ok = authorize(conn, key)
        return bool(await ok) if inspect.isawaitable(ok) else bool(ok)

    async def guard(conn: HTTPConnection, key: str) -> None:
        if not await allowed(conn, key):
            raise HTTPException(404)

    @router.get("/volume/{key}/{name}")
    async def volume(request: Request, key: str, name: str):
        _route_key(key)
        await guard(request, key)
        if name not in VOLUME_FILES:
            raise HTTPException(404)
        f = store.volume_dir(key) / name
        if not f.is_file():
            raise HTTPException(404)
        return FileResponse(
            f,
            media_type="application/octet-stream",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @router.get("/mesh/{key}")
    async def mesh(request: Request, key: str):
        _route_key(key)
        await guard(request, key)
        blob = store.mesh(key)
        if blob is None:  # replaced or removed since the page was told about it
            return Response(status_code=204)
        return Response(
            blob, media_type="application/octet-stream", headers={"Cache-Control": "no-store"}
        )

    @router.get("/img/{name}")
    async def image(request: Request, name: str):
        if not _PNG.fullmatch(name):
            raise HTTPException(404)
        await guard(request, name)
        f = store.images_dir / name
        if not f.is_file():
            raise HTTPException(404)
        return FileResponse(
            f, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"}
        )

    @router.get("/patch/{key}")
    async def patch(request: Request, key: str):
        _route_key(key)
        await guard(request, key)
        blob = store.patch_bundle(key)
        if blob is None:
            return Response(status_code=204)
        return Response(
            blob, media_type="application/octet-stream", headers={"Cache-Control": "no-store"}
        )

    @router.websocket("/patch_ws/{key}")
    async def patch_ws(ws: WebSocket, key: str):
        if not _valid_key(key) or not await allowed(ws, key):
            await ws.close(code=4404)
            return
        await ws.accept()
        sub = store._subscribe(key)
        try:
            while True:
                item = await sub[1].get()
                if item is _OVERFLOW:  # too slow: the client reconnects and resyncs
                    await ws.close(code=4408)
                    break
                await ws.send_bytes(item)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            store._unsubscribe(key, sub)

    return router


def create_api(
    store: ViewerStore, prefix: str = "/api", authorize: Authorize | None = None
) -> FastAPI:
    """Build a FastAPI app with the viewer's routes, for ``rx.App(api_transformer=...)``.

    See :func:`create_router` for ``authorize``.
    """
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    api.include_router(create_router(store, prefix, authorize))
    return api


# ----------------------------------------------- helpers for nninteractive_agent
def volume_meta(meta: dict[str, Any], key: str, prefix: str = "api") -> dict[str, Any]:
    """Build the ``volume_meta`` prop: ``write_volumes``' dict plus ``key`` and the URLs.

    URLs are relative (``api/volume/...``); the component resolves them against the
    Reflex backend.
    """
    _check_key(key)
    return {
        **meta,
        "key": key,
        "url": f"{prefix}/volume/{key}/image.nii.gz",
        "url3d": f"{prefix}/volume/{key}/image3d.nii.gz",
    }


def write_image(store: ViewerStore, img: Any, key: str) -> dict[str, Any]:
    """Write a ``LoadedImage`` with ``layers.write_volumes``; return the ``volume_meta`` prop."""
    from nninteractive_agent.web.layers import write_volumes  # pyright: ignore[reportMissingImports]

    out = store.volume_dir(key)  # validates the key before anything is written
    return volume_meta(write_volumes(img, out), key)


def workspace_patch_bundle(ws: Any) -> bytes:
    """All label layers of an ``AgentWorkspace`` as a patch bundle (``LayerSync`` patches)."""
    from nninteractive_agent.web.layers import LayerSync  # pyright: ignore[reportMissingImports]

    return bundle_patches(LayerSync().patches(ws))


def workspace_mesh(ws: Any, object_id: int) -> bytes | None:
    """Mesh object ``object_id`` of an ``AgentWorkspace`` (a ``crop_mesh`` blob)."""
    from nninteractive_agent.web.layers import (  # pyright: ignore[reportMissingImports]
        array_to_ras,
        crop_mesh,
        object_crops,
    )

    crop = object_crops(ws.instances, [object_id]).get(int(object_id))
    return crop_mesh(crop, array_to_ras(ws.image), ws.image.spacing)


def workspace_objects(ws: Any, palette: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Build the ``objects`` prop for an ``AgentWorkspace``.

    Colors come from ``palette`` (cycled) if given, else from nninteractive_agent's
    ``object_color`` so they match the agent's own renders.
    """
    if palette is not None:
        colors = list(palette)

        def color(i: int) -> str:
            return colors[(i - 1) % len(colors)]
    else:
        from nninteractive_agent.core.render import (  # pyright: ignore[reportMissingImports]
            object_color as color,
        )
    return [
        {"id": int(i), "name": o.get("name", f"object {i}"), "color": color(int(i))}
        for i, o in sorted(ws.objects.items())
    ]


DEFAULT_PALETTE = (
    "#e6194b",
    "#3cb44b",
    "#4363d8",
    "#f58231",
    "#911eb4",
    "#42d4f4",
    "#f032e6",
    "#bfef45",
    "#fabed4",
    "#469990",
)
"""Object colors used when the consuming app does not choose its own."""
