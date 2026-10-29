"""A synthetic CT-like phantom, sphere objects and their surfaces: no GPU, no LLM, no agent.

The same idea as the fake session in nninteractive-agent's ``tests/web_dev.py``, but
self-contained (numpy only): the files are written in exactly the formats
``nninteractive_agent.web.layers`` writes, so the viewer cannot tell the difference.

Workspace arrays are [axis0, axis1, axis2] = reversed NIfTI (i, j, k), so the C-order bytes
of a workspace array are NIfTI voxel order.
"""

from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from reflex_nninteractive_viewer.server import encode_mesh, full_layer_patch

SHAPE = (72, 128, 128)  # axis0 (S), axis1 (A), axis2 (R)
SPACING = (2.5, 1.5, 1.5)  # mm, per workspace axis
ORIGIN = (-96.0, -96.0, -90.0)  # RAS mm of voxel (i, j, k) = (0, 0, 0)
NIFTI_DTYPES = {np.dtype(np.uint8): 2, np.dtype(np.int16): 4, np.dtype(np.uint16): 512}


@dataclass(frozen=True)
class Sphere:
    """An object of the phantom: a sphere around a workspace voxel."""

    id: int
    name: str
    color: str
    center: tuple[int, int, int]  # workspace voxel
    radius_mm: float


SPHERES = (
    Sphere(1, "liver lesion", "#e6194b", (36, 58, 46), 14.0),
    Sphere(2, "kidney cyst", "#3cb44b", (30, 78, 86), 10.0),
    Sphere(3, "spleen nodule", "#4363d8", (44, 52, 88), 8.0),
)


def affine() -> np.ndarray:
    """4x4 NIfTI (i, j, k) -> RAS mm. RAS-aligned: axis2 = R, axis1 = A, axis0 = S."""
    a = np.eye(4)
    a[0, 0], a[1, 1], a[2, 2] = SPACING[2], SPACING[1], SPACING[0]
    a[:3, 3] = ORIGIN
    return a


def voxel_to_ras(p: tuple[float, float, float]) -> np.ndarray:
    """RAS mm of workspace voxel ``p`` = [axis0, axis1, axis2]."""
    return (affine() @ np.array([p[2], p[1], p[0], 1.0]))[:3]


def write_nifti(path: Path, data: np.ndarray) -> None:
    """Write a workspace array as a gzipped NIfTI-1 file with the phantom's sform."""
    hdr = bytearray(352)
    struct.pack_into("<i", hdr, 0, 348)
    nz, ny, nx = data.shape
    struct.pack_into("<8h", hdr, 40, 3, nx, ny, nz, 1, 1, 1, 1)
    struct.pack_into("<hh", hdr, 70, NIFTI_DTYPES[data.dtype], data.dtype.itemsize * 8)
    struct.pack_into("<8f", hdr, 76, 1.0, SPACING[2], SPACING[1], SPACING[0], 0, 0, 0, 0)
    struct.pack_into("<fff", hdr, 108, 352.0, 1.0, 0.0)  # vox_offset, scl_slope, scl_inter
    hdr[123] = 2  # xyzt_units: mm
    struct.pack_into("<hh", hdr, 252, 0, 1)  # qform_code, sform_code (scanner)
    a = affine()
    for r in range(3):
        struct.pack_into("<4f", hdr, 280 + 16 * r, *a[r])
    hdr[344:348] = b"n+1\0"
    with gzip.open(path, "wb", compresslevel=3) as f:
        f.write(bytes(hdr))
        f.write(np.ascontiguousarray(data).astype(data.dtype.newbyteorder("<")).tobytes())


def _grid_mm() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    a0, a1, a2 = np.meshgrid(*(np.arange(n) for n in SHAPE), indexing="ij", sparse=True)
    return a0 * SPACING[0], a1 * SPACING[1], a2 * SPACING[2]


def sphere_mask(s: Sphere) -> np.ndarray:
    """Voxels of sphere ``s``."""
    z, y, x = _grid_mm()
    c = [s.center[k] * SPACING[k] for k in range(3)]
    return (z - c[0]) ** 2 + (y - c[1]) ** 2 + (x - c[2]) ** 2 <= s.radius_mm**2


def phantom() -> np.ndarray:
    """An int16 CT-like volume (HU): air, an elliptic body with fat, organs, spine and spheres."""
    z, y, x = _grid_mm()
    cy, cx = SHAPE[1] * SPACING[1] / 2, SHAPE[2] * SPACING[2] / 2
    rng = np.random.default_rng(7)
    vol = np.full(SHAPE, -1000.0, np.float32)
    body = ((y - cy) / 78) ** 2 + ((x - cx) / 90) ** 2 <= 1
    body = np.broadcast_to(body, SHAPE)
    vol[body] = -90.0  # fat
    inner = np.broadcast_to(((y - cy) / 68) ** 2 + ((x - cx) / 80) ** 2 <= 1, SHAPE)
    vol[inner] = 40.0  # soft tissue
    lungs = (((y - cy - 10) / 40) ** 2 + ((x - cx - 40) / 30) ** 2 <= 1) | (
        ((y - cy - 10) / 40) ** 2 + ((x - cx + 40) / 30) ** 2 <= 1
    )
    lungs = lungs & (z > SHAPE[0] * SPACING[0] * 0.6)
    vol[np.broadcast_to(lungs, SHAPE)] = -850.0
    spine = np.broadcast_to(((y - cy + 50) / 14) ** 2 + ((x - cx) / 14) ** 2 <= 1, SHAPE)
    vol[spine] = 700.0  # bone
    ribs = (np.abs(((y - cy) / 70) ** 2 + ((x - cx) / 82) ** 2 - 1) < 0.06) & (
        (np.round(z / 12) % 2) == 0
    )
    vol[np.broadcast_to(ribs, SHAPE) & np.broadcast_to(body, SHAPE)] = 500.0
    for s, hu in zip(SPHERES, (90.0, 5.0, 120.0), strict=True):
        vol[sphere_mask(s)] = hu
    vol += rng.normal(0, 12, SHAPE).astype(np.float32) * (vol > -900)
    return np.clip(np.round(vol), -1024, 3071).astype(np.int16)


def labels(ids: list[int]) -> np.ndarray:
    """The ``objects`` layer: voxels of each sphere in ``ids`` set to its id."""
    out = np.zeros(SHAPE, np.uint8)
    for s in SPHERES:
        if s.id in ids:
            out[sphere_mask(s) & (out == 0)] = s.id
    return out


def prompts() -> np.ndarray:
    """The ``prompts`` layer: a positive box outline (1) and a negative point (2) on one slice."""
    out = np.zeros(SHAPE, np.uint8)
    s = SPHERES[0]
    z, c1, c2 = s.center
    r = int(s.radius_mm / SPACING[1]) + 3
    out[z, c1 - r : c1 + r + 1, [c2 - r, c2 + r]] = 1
    out[z, [c1 - r, c1 + r], c2 - r : c2 + r + 1] = 1
    out[z, c1 - 1 : c1 + 2, c2 + r + 5 : c2 + r + 8] = 2
    return out


def layer_patches(ids: list[int]) -> list[bytes]:
    """Full-layer patches for objects/current/prompts, as ``LayerSync`` sends them first."""
    return [
        full_layer_patch("objects", labels(ids)),
        full_layer_patch("current", np.zeros(SHAPE, np.uint8)),
        full_layer_patch("prompts", prompts()),
    ]


def sphere_mesh(s: Sphere, rings: int = 24, segments: int = 48) -> bytes:
    """A UV sphere in RAS mm, wound counter-clockwise seen from outside (``crop_mesh`` blob)."""
    c = voxel_to_ras(s.center)
    theta = np.linspace(0, np.pi, rings + 1)
    phi = np.linspace(0, 2 * np.pi, segments, endpoint=False)
    t, p = np.meshgrid(theta, phi, indexing="ij")
    unit = np.stack([np.sin(t) * np.cos(p), np.sin(t) * np.sin(p), np.cos(t)], -1).reshape(-1, 3)
    verts = c + s.radius_mm * unit
    faces = []
    for i in range(rings):
        for j in range(segments):
            a, b = i * segments + j, (i + 1) * segments + j
            d, e = i * segments + (j + 1) % segments, (i + 1) * segments + (j + 1) % segments
            faces += [(a, b, e), (a, e, d)]
    tris = np.array(faces, np.uint32)
    # Outward normals: flip if the enclosed volume comes out negative (as crop_mesh does).
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    if np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum() < 0:
        tris = tris[:, ::-1]
    return encode_mesh(verts.astype(np.float32), np.ascontiguousarray(tris))


def write_volumes(out_dir: Path) -> dict:
    """Write what ``layers.write_volumes`` writes; return its meta dict."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data = phantom()
    write_nifti(out_dir / "image.nii.gz", data)
    write_nifti(out_dir / "image3d.nii.gz", data)
    write_nifti(out_dir / "zeros_uint8.nii.gz", np.zeros(SHAPE, np.uint8))
    write_nifti(out_dir / "zeros_uint16.nii.gz", np.zeros(SHAPE, np.uint16))
    return {
        "shape": list(SHAPE),
        "spacing": list(SPACING),
        "dtype": "int16",
        "is_2d": False,
        "is_ct": True,
        "orientation": ["S", "A", "R"],
        "auto_window": [-160.0, 240.0],
        "range": [float(data.min()), float(data.max())],
        "shrink3d": [1, 1, 1],
    }
