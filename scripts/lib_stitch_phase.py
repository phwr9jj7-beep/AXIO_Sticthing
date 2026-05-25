"""
lib_stitch_phase.py
-------------------
Stitching engine using sub-pixel phase correlation. Calculates pairwise displacements,
solves global positions via least-squares optimization, and blends tiles.
"""

import numpy as np
import tifffile
from pathlib import Path
from tqdm import tqdm
from scipy.optimize import least_squares
from skimage.registration import phase_cross_correlation

from lib_shared import stitch_canvas, save_tiff


def compute_pairwise_shift(tile_a: np.ndarray, tile_b: np.ndarray,
                           direction: str, overlap_px: int) -> tuple[float, float]:
    """Phase correlation shift estimation."""
    if direction == "horizontal":
        strip_a = tile_a[:, -overlap_px:]
        strip_b = tile_b[:, :overlap_px]
    else:  
        strip_a = tile_a[-overlap_px:, :]
        strip_b = tile_b[:overlap_px, :]

    # Limit strip size for speed
    OVERLAP_SAMPLE_PX = 512
    if direction == "horizontal" and strip_a.shape[0] > OVERLAP_SAMPLE_PX:
        mid = strip_a.shape[0] // 2
        half = OVERLAP_SAMPLE_PX // 2
        strip_a = strip_a[mid - half:mid + half, :]
        strip_b = strip_b[mid - half:mid + half, :]
    elif direction == "vertical" and strip_a.shape[1] > OVERLAP_SAMPLE_PX:
        mid = strip_a.shape[1] // 2
        half = OVERLAP_SAMPLE_PX // 2
        strip_a = strip_a[:, mid - half:mid + half]
        strip_b = strip_b[:, mid - half:mid + half]

    try:
        shift, _, _ = phase_cross_correlation(strip_a, strip_b, normalization="phase", upsample_factor=10)
        return float(shift[0]), float(shift[1])
    except Exception:
        return 0.0, 0.0


def global_position_optimisation(grid, tile_h, tile_w, refined_shifts):
    """Solve globally consistent tile positions."""
    keys = sorted(grid.keys())
    idx = {k: i for i, k in enumerate(keys)}
    n = len(keys)

    init_pos = np.zeros((n, 2), dtype=np.float64)
    for (row, col), i in idx.items():
        init_pos[i, 0] = row * tile_h
        init_pos[i, 1] = col * tile_w

    def residuals(pos_flat):
        pos = pos_flat.reshape(n, 2)
        res = []
        for (a, b), (dy, dx) in refined_shifts.items():
            if a in idx and b in idx:
                ia, ib = idx[a], idx[b]
                ra, ca = a
                rb, cb = b
                nominal_dy = (rb - ra) * tile_h
                nominal_dx = (cb - ca) * tile_w
                pred_dy = pos[ib, 0] - pos[ia, 0]
                pred_dx = pos[ib, 1] - pos[ia, 1]
                refined_dy = nominal_dy + dy
                refined_dx = nominal_dx + dx
                res.extend([pred_dy - refined_dy, pred_dx - refined_dx])
        return np.array(res) if res else np.zeros(2)

    if not refined_shifts:
        positions = {}
        for (row, col), i in idx.items():
            positions[(row, col)] = (init_pos[i, 0], init_pos[i, 1])
        return positions

    result = least_squares(residuals, init_pos.flatten(), method="lm", max_nfev=5000)
    opt_pos = result.x.reshape(n, 2)
    positions = {}
    for (row, col), i in idx.items():
        positions[(row, col)] = (opt_pos[i, 0], opt_pos[i, 1])
    return positions


def run_phase_stitch(source_dir: Path, scene_tiles: list, out_path: Path, downsample: int):
    if out_path.exists():
        return
        
    print(f"    [Phase Stitcher] -> {out_path.name}")
    
    tile_w = scene_tiles[0]["w"]
    tile_h = scene_tiles[0]["h"]
    overlap_x = int(tile_w * 0.1)
    overlap_y = int(tile_h * 0.1)
    max_shift_x = int(tile_w * 0.25)
    max_shift_y = int(tile_h * 0.25)
    
    xs = sorted(set(t["x"] for t in scene_tiles))
    ys = sorted(set(t["y"] for t in scene_tiles))
    x_to_col = {x: i for i, x in enumerate(xs)}
    y_to_row = {y: i for i, y in enumerate(ys)}
    grid = {}
    for t in scene_tiles:
        row = y_to_row[t["y"]]
        col = x_to_col[t["x"]]
        grid[(row, col)] = t

    pairs_h, pairs_v = [], []
    for (row, col) in sorted(grid.keys()):
        if (row, col + 1) in grid: pairs_h.append(((row, col), (row, col + 1)))
        if (row + 1, col) in grid: pairs_v.append(((row, col), (row + 1, col)))
    
    all_pairs = pairs_h + pairs_v
    refined_shifts = {}
    for (key_a, key_b) in tqdm(all_pairs, desc="      Phase corr", leave=False):
        path_a = source_dir / grid[key_a]["filename"]
        path_b = source_dir / grid[key_b]["filename"]
        if not path_a.exists() or not path_b.exists(): continue
        
        img_a = tifffile.imread(str(path_a)).astype(np.float32)
        if img_a.ndim > 2: img_a = np.squeeze(img_a); img_a = img_a[..., 0] if img_a.ndim > 2 else img_a
        img_b = tifffile.imread(str(path_b)).astype(np.float32)
        if img_b.ndim > 2: img_b = np.squeeze(img_b); img_b = img_b[..., 0] if img_b.ndim > 2 else img_b
        
        ra, ca = key_a
        rb, cb = key_b
        direction = "horizontal" if cb > ca else "vertical"
        overlap_px = overlap_x if direction == "horizontal" else overlap_y
        
        dy, dx = compute_pairwise_shift(img_a, img_b, direction, overlap_px)
        if abs(dy) > max_shift_y or abs(dx) > max_shift_x:
            dy, dx = 0.0, 0.0
            
        refined_shifts[(key_a, key_b)] = (dy, dx)

    opt_positions = global_position_optimisation(grid, tile_h, tile_w, refined_shifts)
    
    positions = {}
    for (row, col), (abs_y, abs_x) in opt_positions.items():
        positions[grid[(row, col)]["filename"]] = (abs_y, abs_x)
        
    canvas = stitch_canvas(
        positions=positions,
        source_dir=source_dir, 
        tile_list=scene_tiles,
        tile_h=tile_h,
        tile_w=tile_w,
        downsample=downsample
    )
    
    if canvas is not None:
        save_tiff(canvas, out_path)
