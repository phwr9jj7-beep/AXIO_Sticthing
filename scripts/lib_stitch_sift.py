"""
lib_stitch_sift.py
------------------
Stitching engine using SIFT feature detection and RANSAC geometric consensus.
Establishes homography mapping, builds neighbor graphs, and blends tiles.
"""

import numpy as np
import tifffile
from pathlib import Path
from tqdm import tqdm
import cv2
import networkx as nx
from scipy.optimize import least_squares

from lib_shared import stitch_canvas, save_tiff


def normalize_for_sift(img: np.ndarray) -> np.ndarray:
    """Normalize 16-bit to 8-bit for OpenCV SIFT."""
    # Clip to 1st and 99th percentile for contrast
    p1, p99 = np.percentile(img, (1, 99))
    norm = np.clip((img - p1) / (p99 - p1 + 1e-6), 0, 1)
    return (norm * 255).astype(np.uint8)


def compute_sift_shift(img_a: np.ndarray, img_b: np.ndarray, direction: str, overlap_px: int) -> tuple[float, float, int]:
    """
    Returns (dy, dx, inliers_count) describing the shift of img_b relative to img_a.
    Only allows Euclidean (rigid) transforms to prevent scaling/shearing artifacts.
    """
    if direction == "horizontal":
        strip_a = img_a[:, -overlap_px:]
        strip_b = img_b[:, :overlap_px]
        offset_a = (0, img_a.shape[1] - overlap_px)
        offset_b = (0, 0)
    else:
        strip_a = img_a[-overlap_px:, :]
        strip_b = img_b[:overlap_px, :]
        offset_a = (img_a.shape[0] - overlap_px, 0)
        offset_b = (0, 0)

    u8_a = normalize_for_sift(strip_a)
    u8_b = normalize_for_sift(strip_b)

    sift = cv2.SIFT_create()
    kp1, des1 = sift.detectAndCompute(u8_a, None)
    kp2, des2 = sift.detectAndCompute(u8_b, None)

    if des1 is None or des2 is None or len(des1) < 8 or len(des2) < 8:
        return 0.0, 0.0, 0

    # FLANN Matcher
    index_params = dict(algorithm=1, trees=5)
    search_params = dict(checks=50)
    flann = cv2.FlannBasedMatcher(index_params, search_params)
    try:
        matches = flann.knnMatch(des1, des2, k=2)
    except Exception:
        return 0.0, 0.0, 0

    good = []
    for match_tuple in matches:
        if len(match_tuple) == 2:
            m, n = match_tuple
            if m.distance < 0.75 * n.distance:
                good.append(m)

    if len(good) < 8:
        return 0.0, 0.0, len(good)

    src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

    # Convert local strip coordinates to global tile coordinates
    src_pts[:, 0, 0] += offset_a[1]
    src_pts[:, 0, 1] += offset_a[0]
    dst_pts[:, 0, 0] += offset_b[1]
    dst_pts[:, 0, 1] += offset_b[0]

    # Find rigid Euclidean transform (translation + rotation) -> robust to noise
    # We use estimateAffinePartial2D which is similarity (scale+rotation+translation),
    # but microscopy has no scale, so we extract just the translation if it's close to 1.
    M, inliers = cv2.estimateAffinePartial2D(dst_pts, src_pts, method=cv2.RANSAC, ransacReprojThreshold=5.0)
    
    if M is None:
        return 0.0, 0.0, 0
        
    inliers_count = int(np.sum(inliers))
    
    # M maps from B to A:
    # x_A = M[0,0]*x_B + M[0,1]*y_B + M[0,2]
    # y_A = M[1,0]*x_B + M[1,1]*y_B + M[1,2]
    # Assuming rotation is 0 and scale is 1, dy = M[1,2], dx = M[0,2]
    dx = float(M[0, 2])
    dy = float(M[1, 2])
    
    return dy, dx, inliers_count


def run_sift_stitch(source_dir: Path, scene_tiles: list, out_path: Path, downsample: int):
    if out_path.exists():
        return
        
    print(f"    [SIFT Stitcher] -> {out_path.name}")
    
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
    for (key_a, key_b) in tqdm(all_pairs, desc="      SIFT feature matching", leave=False):
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
        
        dy, dx, inliers = compute_sift_shift(img_a, img_b, direction, overlap_px)
        
        # Nominal relative displacement
        nominal_dy = (rb - ra) * tile_h
        nominal_dx = (cb - ca) * tile_w
        
        # SIFT computes shift of B relative to A, so absolute B is A + shift
        # But if inliers < 8, or shift is wildly off, fall back to nominal
        if inliers < 8 or abs(dy - nominal_dy) > max_shift_y or abs(dx - nominal_dx) > max_shift_x:
            dy, dx = nominal_dy, nominal_dx
            
        refined_shifts[(key_a, key_b)] = (dy, dx)

    # ─── Tikhonov-anchored global position solver for SIFT ───
    keys = sorted(grid.keys())
    idx = {k: i for i, k in enumerate(keys)}
    n = len(keys)

    init_pos = np.zeros((n, 2), dtype=np.float64)
    for (row, col), i in idx.items():
        init_pos[i, 0] = row * tile_h
        init_pos[i, 1] = col * tile_w

    if not refined_shifts:
        print("      [SIFT Solver] No shifts detected — falling back to nominal coordinates.")
        opt_pos = init_pos
    else:
        LAMBDA_ANCHOR = 0.5

        def residuals(pos_flat):
            pos = pos_flat.reshape(n, 2)
            res = []
            for (ka, kb), (dy_ref, dx_ref) in refined_shifts.items():
                if ka in idx and kb in idx:
                    ia, ib = idx[ka], idx[kb]
                    res.append(pos[ib, 0] - pos[ia, 0] - dy_ref)
                    res.append(pos[ib, 1] - pos[ia, 1] - dx_ref)
            # Pull solution to stage-coordinate prior to prevent macro drift
            drift = (pos - init_pos) * LAMBDA_ANCHOR
            res.extend(drift.flatten())
            return np.array(res)

        print("      [SIFT Solver] Running Tikhonov-anchored least-squares optimization...")
        result = least_squares(residuals, init_pos.flatten(), method="lm", max_nfev=5000)
        opt_pos = result.x.reshape(n, 2)

    # Map back to filenames
    positions = {}
    for (row, col), i in idx.items():
        positions[grid[(row, col)]["filename"]] = (opt_pos[i, 0], opt_pos[i, 1])
        
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
