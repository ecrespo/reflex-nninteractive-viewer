"""The FastAPI helpers: binary formats, routes, authorization and the patch WebSocket."""

import asyncio
import json
import struct
import subprocess
import sys
import zlib

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from reflex_nninteractive_viewer import server
from reflex_nninteractive_viewer.server import (
    VOLUME_FILES,
    ViewerStore,
    bundle_patches,
    create_api,
    encode_mesh,
    encode_patch,
    full_layer_patch,
    split_bundle,
    volume_meta,
)


def upstream_encode_patch(header, crop):
    """Reference: nninteractive_agent.web.layers.encode_patch (Apache-2.0, MIC-DKFZ), verbatim."""
    head = json.dumps(header).encode()
    body = zlib.compress(np.ascontiguousarray(crop).tobytes(), 1) if crop is not None else b""
    return struct.pack("<I", len(head)) + head + body


def decode_patch(msg: bytes):
    """What viewer.js _applyPatch does: header length, JSON header, inflate the rest."""
    (hlen,) = struct.unpack_from("<I", msg)
    header = json.loads(msg[4 : 4 + hlen])
    return header, zlib.decompress(msg[4 + hlen :])


# ----------------------------------------------------------------------- formats
def test_encode_patch_matches_upstream_byte_for_byte():
    crop = np.arange(2 * 3 * 4, dtype=np.uint16).reshape(2, 3, 4)
    header = {"layer": "objects", "box": [[1, 3], [0, 3], [2, 6]], "dtype": "uint16",
              "shape": [5, 3, 8], "full": False, "seq": 7}  # fmt: skip
    assert encode_patch(header, crop.tobytes()) == upstream_encode_patch(header, crop)


def test_full_layer_patch_round_trips():
    data = (np.arange(4 * 5 * 6) % 3).astype(np.uint8).reshape(4, 5, 6)
    header, raw = decode_patch(full_layer_patch("prompts", data))
    assert header == {"layer": "prompts", "box": [[0, 4], [0, 5], [0, 6]], "dtype": "uint8",
                      "shape": [4, 5, 6], "full": True, "seq": 0}  # fmt: skip
    assert np.array_equal(np.frombuffer(raw, np.uint8).reshape(4, 5, 6), data)


def test_bundle_round_trips_and_detects_truncation():
    parts = [b"a", b"", b"xyz" * 100]
    blob = bundle_patches(parts)
    assert split_bundle(blob) == parts
    assert split_bundle(b"") == []
    with pytest.raises(ValueError, match="truncated"):
        split_bundle(blob[:-1])


def test_mesh_blob_layout_matches_crop_mesh():
    verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], np.float32)
    tris = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]], np.uint32)
    blob = encode_mesh(verts, tris)
    nv, nt = struct.unpack_from("<II", blob)
    assert (nv, nt) == (4, 4)
    assert np.array_equal(np.frombuffer(blob, "<f4", 12, 8).reshape(4, 3), verts)
    assert np.array_equal(np.frombuffer(blob, "<u4", 12, 8 + 48).reshape(4, 3), tris)
    assert len(blob) == 8 + 48 + 48
    # Plain sequences give the same bytes.
    assert encode_mesh(verts.tolist(), tris.tolist()) == blob


def test_volume_meta_adds_key_and_relative_urls():
    meta = volume_meta({"shape": [1, 2, 3], "is_ct": True}, "case-1")
    assert meta["key"] == "case-1"
    assert meta["url"] == "api/volume/case-1/image.nii.gz"
    assert meta["url3d"] == "api/volume/case-1/image3d.nii.gz"
    assert meta["is_ct"] is True


@pytest.mark.parametrize("key", ["../etc", "a/b", ".hidden", "", "x" * 200])
def test_store_rejects_keys_that_could_leave_its_root(tmp_path, key):
    store = ViewerStore(tmp_path)
    with pytest.raises(ValueError, match="invalid key"):
        store.volume_dir(key)
    with pytest.raises(ValueError, match="invalid key"):
        store.set_mesh(key, b"")


# ------------------------------------------------------------------------ routes
@pytest.fixture
def store(tmp_path):
    s = ViewerStore(tmp_path)
    d = s.volume_dir("img1")
    d.mkdir()
    for name in VOLUME_FILES:
        (d / name).write_bytes(name.encode())
    (d / "secret.txt").write_text("no")
    s.images_dir.mkdir()
    (s.images_dir / "00001.png").write_bytes(b"\x89PNG")
    return s


@pytest.fixture
def client(store):
    return TestClient(create_api(store))


@pytest.mark.parametrize("name", VOLUME_FILES)
def test_volume_files_are_served(client, name):
    r = client.get(f"/api/volume/img1/{name}")
    assert r.status_code == 200
    assert r.content == name.encode()
    assert r.headers["content-type"] == "application/octet-stream"


@pytest.mark.parametrize(
    "path",
    [
        "/api/volume/img1/secret.txt",
        "/api/volume/nope/image.nii.gz",
        "/api/volume/.img1/image.nii.gz",
        "/api/volume/img1/..%2Fsecret.txt",
        "/api/img/../img1/secret.txt",
        "/api/img/00002.png",
        "/api/img/x.txt",
    ],
)
def test_other_paths_are_not_served(client, path):
    assert client.get(path).status_code == 404


def test_meshes_are_versioned_and_204_when_gone(store, client):
    assert client.get("/api/mesh/o1").status_code == 204
    assert store.set_mesh("o1", b"one") == 1
    assert store.set_mesh("o1", b"two") == 2
    assert store.mesh_version("o1") == 2
    r = client.get("/api/mesh/o1?v=2")
    assert r.status_code == 200 and r.content == b"two"
    assert r.headers["cache-control"] == "no-store"
    store.remove_mesh("o1")
    assert client.get("/api/mesh/o1").status_code == 204


def test_agent_images_are_served(client):
    r = client.get("/api/img/00001.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_patch_bundle_route(store, client):
    assert client.get("/api/patch/img1").status_code == 204
    blob = bundle_patches([full_layer_patch("objects", np.zeros((2, 2, 2), np.uint8))])
    store.set_patch_bundle("img1", blob)
    r = client.get("/api/patch/img1")
    assert r.status_code == 200 and r.content == blob


def test_patch_websocket_streams_published_patches(store, client):
    with client.websocket_connect("/api/patch_ws/img1") as ws:
        store.publish("img1", b"patch-1")
        store.publish("other", b"not for us")
        store.publish("img1", b"patch-2")
        assert ws.receive_bytes() == b"patch-1"
        assert ws.receive_bytes() == b"patch-2"


def test_custom_prefix(store):
    c = TestClient(create_api(store, prefix="/viewer"))
    assert c.get("/viewer/volume/img1/image.nii.gz").status_code == 200
    assert c.get("/api/volume/img1/image.nii.gz").status_code == 404


# ------------------------------------------------------------------ authorization
@pytest.mark.parametrize("asynchronous", [False, True])
def test_authorize_guards_every_route(store, asynchronous):
    seen = []

    def check(conn, key):
        seen.append(key)
        return conn.headers.get("x-user") == "ok"

    async def acheck(conn, key):
        return check(conn, key)

    store.set_mesh("o1", b"m")
    store.set_patch_bundle("img1", b"b")
    c = TestClient(create_api(store, authorize=acheck if asynchronous else check))
    paths = [
        "/api/volume/img1/image.nii.gz",
        "/api/mesh/o1",
        "/api/img/00001.png",
        "/api/patch/img1",
    ]
    for path in paths:
        assert c.get(path).status_code == 404, path
        assert c.get(path, headers={"x-user": "ok"}).status_code == 200, path
    assert {"img1", "o1", "00001.png"} <= set(seen)
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/patch_ws/img1") as ws:
            ws.receive_bytes()
    assert exc.value.code == 4404
    with c.websocket_connect("/api/patch_ws/img1", headers={"x-user": "ok"}):
        pass


def test_slow_subscribers_are_dropped_not_buffered(monkeypatch):
    monkeypatch.setattr(server, "MAX_QUEUED_PATCHES", 3)

    async def run():
        queue = asyncio.Queue(maxsize=server.MAX_QUEUED_PATCHES + 1)
        for i in range(10):
            server._offer(queue, bytes([i]))
        return [queue.get_nowait() for _ in range(queue.qsize())]

    items = asyncio.run(run())
    assert items[:3] == [b"\x00", b"\x01", b"\x02"]
    assert items[3] is server._OVERFLOW
    assert len(items) == 4


# --------------------------------------------------------------- optional agent
def test_module_does_not_import_nninteractive_agent():
    code = (
        "import sys, reflex_nninteractive_viewer.server, reflex_nninteractive_viewer; "
        "print('nninteractive_agent' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("nninteractive_agent") is not None,
    reason="nninteractive_agent is installed",
)
@pytest.mark.parametrize(
    "call",
    [
        lambda s: server.write_image(s, object(), "k"),
        lambda s: server.workspace_patch_bundle(object()),
        lambda s: server.workspace_mesh(object(), 1),
        lambda s: server.workspace_objects(object()),
    ],
)
def test_agent_helpers_need_nninteractive_agent_only_when_called(tmp_path, call):
    with pytest.raises(ModuleNotFoundError, match="nninteractive_agent"):
        call(ViewerStore(tmp_path))


def test_workspace_objects_with_a_palette_does_not_need_the_agent():
    class WS:
        objects = {2: {"name": "kidney"}, 1: {"name": "liver"}}

    assert server.workspace_objects(WS(), palette=["#111111", "#222222"]) == [
        {"id": 1, "name": "liver", "color": "#111111"},
        {"id": 2, "name": "kidney", "color": "#222222"},
    ]
