"""
stitchers.py
------------
Tile registration / alignment algorithms for AXIO stitching.

Extracted verbatim from gui_runner.py:
  - construct_grid()              lines 441-457
  - solve_optimal_positions()     lines 407-439 (Tikhonov-anchored least-squares)
  - run_bounded_phase_corr()      lines 459-542
  - run_sift_alignment()          lines 545-617

All three algorithms are exposed through the unified compute_alignment() facade.
The LAMBDA_ANCHOR and MAX_PHASE_SHIFT constants are preserved as-is.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import numpy as np
from scipy.optimize import least_squares
from skimage.registration import phase_cross_correlation

from .canvas import detect_tile_axes, read_tile_frame
from .models import ProgressCallback, ProgressEvent, PipelineStage


# Constants — preserved from gui_runner.py
MAX_PHASE_SHIFT: int = 25
LAMBDA_ANCHOR: float = 0.5



def _log(message: str) -> None:
    """
    Emit a diagnostic line on STDERR.

    Never stdout: when this pipeline runs as an MCP stdio server, stdout IS the JSON-RPC
    transport and a stray line corrupts the frame stream. The GUI worker merges stderr into
    stdout, so its log view is unaffected.
    """
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def compute_alignment(
    tiles: list[dict],
    source_dir: Path,
    algorithm: str,
    ref_channel_idx: int = 0,
    alignment_mode: str = "reference",
    z_mode: str = "none",
    ref_z_slice: int = 0,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, tuple[float, float]]:
    """
    Compute global tile positions using the specified algorithm.

    Returns a dict mapping reference `filename` -> (abs_y, abs_x).

    algorithm options: 'phase' | 'sift' | 'coordinate'
    """
    def _progress(percent: int, msg: str = "") -> None:
        if progress_callback:
            progress_callback(ProgressEvent(
                percent=percent, status_message=msg, stage=PipelineStage.ALIGNMENT
            ))
        else:
            _log(f"[STATUS] {msg}")
            _log(f"[PROGRESS] {percent}")

    if algorithm == "phase":
        return _run_bounded_phase_corr(
            tiles, source_dir,
            ref_channel_idx=ref_channel_idx,
            alignment_mode=alignment_mode,
            z_mode=z_mode,
            ref_z_slice=ref_z_slice,
            progress_fn=_progress,
        )
    elif algorithm == "sift":
        return _run_sift_alignment(
            tiles, source_dir,
            ref_channel_idx=ref_channel_idx,
            alignment_mode=alignment_mode,
            z_mode=z_mode,
            ref_z_slice=ref_z_slice,
            progress_fn=_progress,
        )
    else:  # coordinate
        _progress(80, "Using coordinates directly from Zeiss stage limits.")
        return {t["filename"]: (t["y"], t["x"]) for t in tiles}


# ---------------------------------------------------------------------------
# Grid construction utility (shared by phase and SIFT)
# ---------------------------------------------------------------------------

def construct_grid(tiles: list[dict]) -> tuple[dict, dict]:
    """
    Construct a discrete row/col layout from nominal tile stage positions.

    Returns (grid, idx_map):
        grid    – dict (row, col) -> tile dict
        idx_map – dict (row, col) -> integer index (for least_squares)

    Preserved verbatim from gui_runner.py lines 441-457.
    """
    xs = sorted(set(t["x"] for t in tiles))
    ys = sorted(set(t["y"] for t in tiles))

    step_x = float(np.median(np.diff(xs))) if len(xs) > 1 else 1020.0
    step_y = float(np.median(np.diff(ys))) if len(ys) > 1 else 1020.0
    min_x, min_y = xs[0], ys[0]

    grid: dict = {}
    idx_map: dict = {}
    for i, t in enumerate(tiles):
        col = int(round((t["x"] - min_x) / step_x))
        row = int(round((t["y"] - min_y) / step_y))
        grid[(row, col)] = t
        idx_map[(row, col)] = i
    return grid, idx_map


# ---------------------------------------------------------------------------
# Tikhonov-anchored global least-squares solver (shared by phase and SIFT)
# ---------------------------------------------------------------------------

def solve_optimal_positions(
    grid: dict,
    idx_map: dict,
    refined_shifts: dict,
    tiles: list[dict],
    progress_fn: Callable | None = None,
) -> dict[str, tuple[float, float]]:
    """
    Run global least-squares optimisation using Tikhonov regularization anchored
    to the nominal stage coordinates.

    Preserved verbatim from gui_runner.py lines 407-439.
    LAMBDA_ANCHOR = 0.5 (see module constant).
    """
    n = len(tiles)
    init_pos = np.zeros((n, 2), dtype=np.float64)
    for (row, col), t in grid.items():
        i = idx_map[(row, col)]
        init_pos[i, 0] = t["y"]
        init_pos[i, 1] = t["x"]

    if not refined_shifts:
        if progress_fn:
            progress_fn(80, "No shifts detected. Stitching falls back to coordinate alignment.")
        return {t["filename"]: (t["y"], t["x"]) for t in tiles}

    def residuals(pos_flat: np.ndarray) -> np.ndarray:
        pos = pos_flat.reshape(n, 2)
        res = []
        for (ka, kb), (dy_ref, dx_ref) in refined_shifts.items():
            ia, ib = idx_map[ka], idx_map[kb]
            res.append(pos[ib, 0] - pos[ia, 0] - dy_ref)
            res.append(pos[ib, 1] - pos[ia, 1] - dx_ref)
        drift = (pos - init_pos) * LAMBDA_ANCHOR
        res.extend(drift.flatten())
        return np.array(res)

    if progress_fn:
        progress_fn(78, "Executing Tikhonov-anchored least-squares optimization...")
    result = least_squares(residuals, init_pos.flatten(), method="lm", max_nfev=5000)
    opt = result.x.reshape(n, 2)

    positions: dict[str, tuple[float, float]] = {}
    for (row, col), t in grid.items():
        i = idx_map[(row, col)]
        positions[t["filename"]] = (opt[i, 0], opt[i, 1])
    return positions


# ---------------------------------------------------------------------------
# Phase correlation algorithm
# ---------------------------------------------------------------------------

def _run_bounded_phase_corr(
    tiles: list[dict],
    source_dir: Path,
    ref_channel_idx: int = 0,
    alignment_mode: str = "reference",
    z_mode: str = "none",
    ref_z_slice: int = 0,
    progress_fn: Callable | None = None,
) -> dict[str, tuple[float, float]]:
    """
    Perform bounded phase correlation on tile overlaps.

    Preserved verbatim from gui_runner.py lines 459-542.
    """
    grid, idx_map = construct_grid(tiles)
    pairs = []
    for (row, col) in sorted(grid.keys()):
        if (row, col + 1) in grid:
            pairs.append(("horizontal", (row, col), (row, col + 1)))
        if (row + 1, col) in grid:
            pairs.append(("vertical", (row, col), (row + 1, col)))

    sample_info = detect_tile_axes(source_dir / tiles[0]["filename"])
    tile_h = sample_info["H"]
    tile_w = sample_info["W"]

    refined_shifts: dict = {}
    n_pairs = len(pairs)
    if progress_fn:
        progress_fn(50, f"Computing {n_pairs} pairwise phase-correlations...")

    if z_mode == "none":
        z_read_mode = "slice"
        reg_z_idx = 0
    elif z_mode == "ref_slice_3d":
        z_read_mode = "slice"
        reg_z_idx = ref_z_slice
    else:
        z_read_mode = "mip"
        reg_z_idx = 0

    for idx, (direction, ka, kb) in enumerate(pairs):
        pa = source_dir / grid[ka]["filename"]
        pb = source_dir / grid[kb]["filename"]
        if not pa.exists() or not pb.exists():
            continue

        a_geo, b_geo = grid[ka], grid[kb]
        dx_nom = b_geo["x"] - a_geo["x"]
        dy_nom = b_geo["y"] - a_geo["y"]

        ov_x = max(0, int(tile_w - dx_nom)) if direction == "horizontal" else tile_w
        ov_y = max(0, int(tile_h - dy_nom)) if direction == "vertical" else tile_h

        if ov_x <= 0 or ov_y <= 0:
            continue

        img_a = read_tile_frame(pa, channel_idx=ref_channel_idx, z_idx=reg_z_idx,
                                channel_mode=alignment_mode, z_mode=z_read_mode).astype(np.float32)
        img_b = read_tile_frame(pb, channel_idx=ref_channel_idx, z_idx=reg_z_idx,
                                channel_mode=alignment_mode, z_mode=z_read_mode).astype(np.float32)

        if direction == "horizontal":
            sa = img_a[:, -ov_x:]
            sb = img_b[:, :ov_x]
        else:
            sa = img_a[-ov_y:, :]
            sb = img_b[:ov_y, :]

        MAX_D = 800
        if direction == "horizontal" and sa.shape[0] > MAX_D:
            mid = sa.shape[0] // 2
            half = MAX_D // 2
            sa = sa[mid - half:mid + half, :]
            sb = sb[mid - half:mid + half, :]
        elif direction == "vertical" and sa.shape[1] > MAX_D:
            mid = sa.shape[1] // 2
            half = MAX_D // 2
            sa = sa[:, mid - half:mid + half]
            sb = sb[:, mid - half:mid + half]

        try:
            shift, _, _ = phase_cross_correlation(sa, sb, normalization="phase", upsample_factor=10)
            dy, dx = float(shift[0]), float(shift[1])
            if abs(dy) > MAX_PHASE_SHIFT or abs(dx) > MAX_PHASE_SHIFT:
                dy, dx = 0.0, 0.0
        except Exception:
            dy, dx = 0.0, 0.0

        refined_shifts[(ka, kb)] = (dy_nom + dy, dx_nom + dx)

        if idx % max(1, n_pairs // 20) == 0 and progress_fn:
            progress_fn(int(50 + (idx / n_pairs) * 28), "")

    return solve_optimal_positions(grid, idx_map, refined_shifts, tiles, progress_fn)


# ---------------------------------------------------------------------------
# SIFT feature-based alignment algorithm
# ---------------------------------------------------------------------------

def _run_sift_alignment(
    tiles: list[dict],
    source_dir: Path,
    ref_channel_idx: int = 0,
    alignment_mode: str = "reference",
    z_mode: str = "none",
    ref_z_slice: int = 0,
    progress_fn: Callable | None = None,
) -> dict[str, tuple[float, float]]:
    """
    Perform SIFT feature-based alignment on tile overlaps.

    Requires lib_stitch_sift.compute_sift_shift (in scripts/).
    Preserved verbatim from gui_runner.py lines 545-617.
    """
    # lib_stitch_sift is in scripts/ — ensure it is findable
    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from lib_stitch_sift import compute_sift_shift  # noqa: PLC0415

    grid, idx_map = construct_grid(tiles)
    pairs = []
    for (row, col) in sorted(grid.keys()):
        if (row, col + 1) in grid:
            pairs.append(("horizontal", (row, col), (row, col + 1)))
        if (row + 1, col) in grid:
            pairs.append(("vertical", (row, col), (row + 1, col)))

    sample_info = detect_tile_axes(source_dir / tiles[0]["filename"])
    tile_h = sample_info["H"]
    tile_w = sample_info["W"]
    max_shift_x = int(tile_w * 0.25)
    max_shift_y = int(tile_h * 0.25)

    refined_shifts: dict = {}
    n_pairs = len(pairs)
    if progress_fn:
        progress_fn(50, f"Computing {n_pairs} pairwise SIFT alignments...")

    if z_mode == "none":
        z_read_mode = "slice"
        reg_z_idx = 0
    elif z_mode == "ref_slice_3d":
        z_read_mode = "slice"
        reg_z_idx = ref_z_slice
    else:
        z_read_mode = "mip"
        reg_z_idx = 0

    for idx, (direction, ka, kb) in enumerate(pairs):
        pa = source_dir / grid[ka]["filename"]
        pb = source_dir / grid[kb]["filename"]
        if not pa.exists() or not pb.exists():
            continue

        a_geo, b_geo = grid[ka], grid[kb]
        dx_nom = b_geo["x"] - a_geo["x"]
        dy_nom = b_geo["y"] - a_geo["y"]

        ov_x = max(0, int(tile_w - dx_nom)) if direction == "horizontal" else tile_w
        ov_y = max(0, int(tile_h - dy_nom)) if direction == "vertical" else tile_h
        overlap_px = ov_x if direction == "horizontal" else ov_y
        if overlap_px <= 0:
            continue

        img_a = read_tile_frame(pa, channel_idx=ref_channel_idx, z_idx=reg_z_idx,
                                channel_mode=alignment_mode, z_mode=z_read_mode).astype(np.float32)
        img_b = read_tile_frame(pb, channel_idx=ref_channel_idx, z_idx=reg_z_idx,
                                channel_mode=alignment_mode, z_mode=z_read_mode).astype(np.float32)

        try:
            dy, dx, inliers = compute_sift_shift(img_a, img_b, direction, overlap_px)
            if inliers < 8 or abs(dy - dy_nom) > max_shift_y or abs(dx - dx_nom) > max_shift_x:
                dy, dx = dy_nom, dx_nom
        except Exception:
            dy, dx = dy_nom, dx_nom

        refined_shifts[(ka, kb)] = (dy, dx)

        if idx % max(1, n_pairs // 20) == 0 and progress_fn:
            progress_fn(int(50 + (idx / n_pairs) * 28), "")

    return solve_optimal_positions(grid, idx_map, refined_shifts, tiles, progress_fn)
