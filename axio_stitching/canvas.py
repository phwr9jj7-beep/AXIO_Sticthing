"""
canvas.py
---------
Canvas assembly, tile reading, blending, and output utilities.

Migrated verbatim from lib_shared.py — all function signatures, algorithm
constants, and output format are preserved exactly. This is the authoritative
copy; lib_shared.py becomes a thin re-export shim.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import tifffile
from tqdm import tqdm



def _log(message: str) -> None:
    """
    Emit a diagnostic line on STDERR.

    Never stdout: when this pipeline runs as an MCP stdio server, stdout IS the JSON-RPC
    transport and a stray line corrupts the frame stream. The GUI worker merges stderr into
    stdout, so its log view is unaffected.
    """
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Tile axis detection
# ---------------------------------------------------------------------------

def detect_tile_axes(tif_path: Path) -> dict:
    """
    Detect tile TIFF dimensions using tifffile metadata.

    Returns dict with keys: 'axes', 'shape', 'num_channels', 'num_z', 'H', 'W'

    Preserved verbatim from lib_shared.py lines 46-99.
    """
    try:
        with tifffile.TiffFile(str(tif_path)) as tif:
            s = tif.series[0]
            axes = s.axes.upper()
            axes = axes.replace("S", "C")
            shape = s.shape

            dim_dict = {axes[i]: shape[i] for i in range(len(axes))}
            num_channels = dim_dict.get("C", 1)
            num_z = dim_dict.get("Z", 1)
            H = dim_dict.get("Y", shape[-2] if len(shape) >= 2 else shape[0])
            W = dim_dict.get("X", shape[-1] if len(shape) >= 1 else shape[0])

            if not axes or len(axes) != len(shape):
                raise ValueError("Unrecognized axes format")

            return {
                "axes": axes,
                "shape": shape,
                "num_channels": num_channels,
                "num_z": num_z,
                "H": H,
                "W": W,
            }
    except Exception:
        pass

    # Fallback to legacy shape heuristic
    try:
        img = tifffile.imread(str(tif_path))
        shape = img.shape
        ndim = img.ndim
        if ndim == 2:
            return {"axes": "YX", "shape": shape, "num_channels": 1, "num_z": 1,
                    "H": shape[0], "W": shape[1]}
        elif ndim == 3:
            if shape[0] < shape[2]:
                return {"axes": "CYX", "shape": shape, "num_channels": shape[0],
                        "num_z": 1, "H": shape[1], "W": shape[2]}
            else:
                return {"axes": "YXC", "shape": shape, "num_channels": shape[2],
                        "num_z": 1, "H": shape[0], "W": shape[1]}
    except Exception:
        pass

    return {"axes": "YX", "shape": (1020, 1020), "num_channels": 1, "num_z": 1,
            "H": 1020, "W": 1020}


# ---------------------------------------------------------------------------
# Tile frame reading
# ---------------------------------------------------------------------------

def read_tile_frame(
    tif_path: Path,
    channel_idx: int = 0,
    z_idx: int = 0,
    channel_mode: str = "reference",
    z_mode: str = "slice",
) -> np.ndarray:
    """
    Read a 2D frame from a multi-dimensional TIFF tile.

    channel_mode: 'reference' | 'average' | 'max_projection'
    z_mode:       'slice' | 'mip'

    Returns: 2D np.ndarray (H, W)

    Preserved verbatim from lib_shared.py lines 102-165.
    """
    info = detect_tile_axes(tif_path)
    axes = info["axes"]
    shape = info["shape"]

    img = tifffile.imread(str(tif_path))
    axis_to_idx = {axes[i]: i for i in range(len(axes))}

    if len(axes) == 2 and "Y" in axis_to_idx and "X" in axis_to_idx:
        return np.squeeze(img)

    # Handle Z axis
    if "Z" in axis_to_idx:
        z_pos = axis_to_idx["Z"]
        if z_mode == "mip":
            img = np.max(img, axis=z_pos)
            axes = axes[:z_pos] + axes[z_pos + 1:]
            axis_to_idx = {axes[i]: i for i in range(len(axes))}
        else:
            z_len = shape[z_pos]
            z_idx_clamped = max(0, min(z_idx, z_len - 1))
            slicer = [slice(None)] * img.ndim
            slicer[z_pos] = z_idx_clamped
            img = img[tuple(slicer)]
            axes = axes[:z_pos] + axes[z_pos + 1:]
            axis_to_idx = {axes[i]: i for i in range(len(axes))}

    # Handle C axis
    if "C" in axis_to_idx:
        c_pos = axis_to_idx["C"]
        if channel_mode == "max_projection":
            img = np.max(img, axis=c_pos)
            axes = axes[:c_pos] + axes[c_pos + 1:]
        elif channel_mode == "average":
            img = np.mean(img, axis=c_pos)
            axes = axes[:c_pos] + axes[c_pos + 1:]
        else:  # reference
            c_len = img.shape[c_pos]
            c_idx_clamped = max(0, min(channel_idx, c_len - 1))
            slicer = [slice(None)] * img.ndim
            slicer[c_pos] = c_idx_clamped
            img = img[tuple(slicer)]
            axes = axes[:c_pos] + axes[c_pos + 1:]

    return np.squeeze(img)


def read_tile_channel(tif_path: Path, channel_idx: int = 0) -> np.ndarray:
    """Read a specific channel from a TIFF tile. Backward-compat wrapper."""
    return read_tile_frame(tif_path, channel_idx=channel_idx, z_idx=0,
                           channel_mode="reference", z_mode="slice")


# ---------------------------------------------------------------------------
# Filename resolution for split channels / Z-slices
# ---------------------------------------------------------------------------

def resolve_tile_filename(
    base_fn: str,
    channel_tag: str | None = None,
    z_idx: int | None = None,
    ref_tag: str | None = None,
) -> str:
    """
    Resolve a tile filename for a target channel tag and/or Z-slice index.

    Preserved verbatim from lib_shared.py lines 190-217.
    """
    fn = base_fn

    if ref_tag and channel_tag:
        fn = fn.replace(ref_tag, channel_tag)

    z_pattern = re.compile(r"_z(\d+)_")
    m = z_pattern.search(fn)
    if m:
        digits_len = len(m.group(1))
        z_str = f"_z{z_idx:0{digits_len}d}_"
        fn = z_pattern.sub(z_str, fn)
    elif z_idx is not None and z_idx > 0:
        if "_ORG.tif" in fn:
            fn = fn.replace("_ORG.tif", f"_z{z_idx:02d}_ORG.tif")
        else:
            parts = fn.rsplit(".", 1)
            if len(parts) == 2:
                fn = f"{parts[0]}_z{z_idx:02d}.{parts[1]}"
    return fn


# ---------------------------------------------------------------------------
# Feathered blending weight mask
# ---------------------------------------------------------------------------

def make_feather_weight(tile_h: int, tile_w: int, border_frac: float = 0.12) -> np.ndarray:
    """
    Create a float32 weight mask with linear feathering at borders.

    Preserved verbatim from lib_shared.py lines 173-187.
    """
    border_y = max(1, int(tile_h * border_frac))
    border_x = max(1, int(tile_w * border_frac))
    wy = np.ones(tile_h, dtype=np.float32)
    wx = np.ones(tile_w, dtype=np.float32)
    for i in range(border_y):
        v = (i + 1) / (border_y + 1)
        wy[i] = v
        wy[tile_h - 1 - i] = v
    for j in range(border_x):
        v = (j + 1) / (border_x + 1)
        wx[j] = v
        wx[tile_w - 1 - j] = v
    return np.outer(wy, wx)


# ---------------------------------------------------------------------------
# Canvas assembly — feathered blending
# ---------------------------------------------------------------------------

def stitch_canvas(
    positions: dict,
    source_dir: Path,
    tile_list: list,
    tile_h: int,
    tile_w: int,
    downsample: int,
    channel_idx: int = 0,
    z_idx: int = 0,
    read_func=None,
    channel_mode: str = "reference",
    z_mode: str = "slice",
    ref_tag: str = "",
    channel_tag: str = "",
) -> np.ndarray | None:
    """
    Assemble a unified 16-bit canvas using global coordinates and feathered blending.

    positions: dict mapping reference `filename` -> (abs_y, abs_x)
    source_dir: directory where (corrected) tiles are stored
    tile_list: list of tile dicts for this scene

    Preserved verbatim from lib_shared.py lines 220-293.
    """
    _log(f"      Assembling canvas with {len(positions)} tiles at {downsample}x downsample...")

    abs_ys = [y for y, x in positions.values()]
    abs_xs = [x for y, x in positions.values()]
    y_min, x_min = min(abs_ys), min(abs_xs)
    y_max, x_max = max(abs_ys), max(abs_xs)

    canvas_h_full = int((y_max - y_min) + tile_h)
    canvas_w_full = int((x_max - x_min) + tile_w)

    canvas_h = canvas_h_full // downsample
    canvas_w = canvas_w_full // downsample
    tile_h_ds = tile_h // downsample
    tile_w_ds = tile_w // downsample

    accumulator = np.zeros((canvas_h, canvas_w), dtype=np.float32)
    weight_map = np.zeros((canvas_h, canvas_w), dtype=np.float32)
    tile_weight = make_feather_weight(tile_h_ds, tile_w_ds)

    files_processed = 0
    for t in tqdm(tile_list, desc="      Blending", leave=False):
        ref_fn = t["filename"]
        if ref_fn not in positions:
            continue

        fn = resolve_tile_filename(ref_fn, channel_tag=channel_tag,
                                   z_idx=z_idx, ref_tag=ref_tag)
        tif_path = source_dir / fn
        if not tif_path.exists():
            continue

        if read_func is not None:
            tile_img = read_func(tif_path, channel_idx=channel_idx, z_idx=z_idx,
                                 channel_mode=channel_mode, z_mode=z_mode).astype(np.float32)
        else:
            tile_img = read_tile_frame(tif_path, channel_idx=channel_idx, z_idx=z_idx,
                                       channel_mode=channel_mode, z_mode=z_mode).astype(np.float32)

        if downsample > 1:
            from skimage.transform import downscale_local_mean
            tile_img = downscale_local_mean(tile_img, (downsample, downsample)).astype(np.float32)
            tile_img = tile_img[:tile_h_ds, :tile_w_ds]

        abs_y, abs_x = positions[ref_fn]
        cy = int((abs_y - y_min) / downsample)
        cx = int((abs_x - x_min) / downsample)
        th, tw = tile_img.shape

        cy2 = min(cy + th, canvas_h)
        cx2 = min(cx + tw, canvas_w)
        tile_img = tile_img[: cy2 - cy, : cx2 - cx]
        w = tile_weight[: cy2 - cy, : cx2 - cx]

        accumulator[cy:cy2, cx:cx2] += tile_img * w
        weight_map[cy:cy2, cx:cx2] += w
        files_processed += 1

    if files_processed == 0:
        return None

    valid = weight_map > 0
    canvas = np.zeros_like(accumulator)
    canvas[valid] = accumulator[valid] / weight_map[valid]
    canvas = np.clip(canvas, 0, 65535).astype(np.uint16)
    return canvas


# ---------------------------------------------------------------------------
# TIFF output
# ---------------------------------------------------------------------------

def save_tiff(canvas: np.ndarray, out_path: Path, axes_hint: str | None = None) -> None:
    """
    Save canvas as an ImageJ-compatible 16-bit compressed TIFF.

    Preserved verbatim from lib_shared.py lines 296-321.
    """
    if axes_hint is not None:
        axes_meta = axes_hint
    else:
        if canvas.ndim == 4:
            axes_meta = "ZCYX"
        elif canvas.ndim == 3:
            axes_meta = "CYX" if canvas.shape[0] <= 5 else "ZYX"
        else:
            axes_meta = "YX"

    tifffile.imwrite(
        str(out_path),
        canvas,
        imagej=True,
        photometric="minisblack",
        compression="deflate",
        metadata={"axes": axes_meta, "mode": "composite"},
    )
    _log(f"      Saved compressed {axes_meta} [{canvas.shape}] -> {out_path.name}")


# ---------------------------------------------------------------------------
# Preview thumbnail generation
# ---------------------------------------------------------------------------

def save_preview_thumbnail(
    canvas: np.ndarray,
    out_path: Path,
    num_channels: int = 1,
    num_z: int = 1,
    stitching_z_mode: str = "slice",
) -> None:
    """
    Generate a lightweight 8-bit visual preview PNG and save it.

    Preserved verbatim from lib_shared.py lines 324-395.
    """
    try:
        from PIL import Image

        if num_channels == 3:
            if canvas.ndim == 4:
                z_mid = canvas.shape[0] // 2
                img_3c = canvas[z_mid]
            else:
                img_3c = canvas

            img_rgb = np.moveaxis(img_3c, 0, 2)
            h, w = img_rgb.shape[:2]
            max_dim = 1024
            if h > max_dim or w > max_dim:
                ds_factor = max(1, int(round(max(h, w) / max_dim)))
                if ds_factor > 1:
                    img_rgb = img_rgb[::ds_factor, ::ds_factor, :]

            img_8bit = np.zeros_like(img_rgb, dtype=np.uint8)
            for c in range(3):
                ch_data = img_rgb[:, :, c].astype(np.float32)
                p99 = np.percentile(ch_data, 99.5) if ch_data.size > 0 else 0
                if p99 > 0:
                    ch_data = np.clip(ch_data / p99 * 255.0, 0, 255)
                img_8bit[:, :, c] = ch_data.astype(np.uint8)

            pil_img = Image.fromarray(img_8bit, mode="RGB")
        else:
            img_2d = canvas
            if canvas.ndim == 4:
                z_mid = canvas.shape[0] // 2
                img_2d = canvas[z_mid, 0]
            elif canvas.ndim == 3:
                if num_channels > 1:
                    img_2d = canvas[0]
                else:
                    z_mid = canvas.shape[0] // 2
                    img_2d = canvas[z_mid]

            img_2d = np.squeeze(img_2d)
            if img_2d.ndim > 2:
                img_2d = img_2d[0]

            h, w = img_2d.shape
            max_dim = 1024
            if h > max_dim or w > max_dim:
                ds_factor = max(1, int(round(max(h, w) / max_dim)))
                if ds_factor > 1:
                    img_2d = img_2d[::ds_factor, ::ds_factor]

            img_min = img_2d.min()
            img_max = img_2d.max()
            if img_max > img_min:
                img_8 = ((img_2d - img_min) / (img_max - img_min) * 255).astype(np.uint8)
            else:
                img_8 = img_2d.astype(np.uint8)

            img_rgb = np.stack([img_8, img_8, img_8], axis=-1)
            pil_img = Image.fromarray(img_rgb)

        preview_path = out_path.with_name(out_path.stem + "_preview.png")
        pil_img.save(str(preview_path), "PNG")
        _log(f"      Saved preview thumbnail -> {preview_path.name}")
    except Exception as e:
        _log(f"      Warning: Failed to generate preview thumbnail: {e}")
