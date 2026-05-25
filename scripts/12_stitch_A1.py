"""
12_stitch_A1.py
---------------
Dedicated stitcher for A1-Image Export-01, whose _info.xml is empty (ZEN export bug).
Uses _meta.xml grid geometry (ContourSize / Columns / Rows / scale) to reconstruct
meander-order tile positions, then applies the same BaSiCPy + Bounded-Phase-Correlation
pipeline used for the other wells (script 11).

Key design insight:
  - The actual file count per scene DIFFERS from (cols*rows) because ZEN skipped
    edge grid positions for non-rectangular scan areas.
  - Tiles are numbered m001..mN sequentially in meander order, covering only
    *acquired* positions. We assign grid positions in meander order and skip
    positions that are off-tissue by checking if the file actually exists.
  - Phase correlation with stage-coordinate anchoring finds sub-pixel shifts
    within each overlap zone.
"""

import os
import sys
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import numpy as np
import tifffile
from tqdm import tqdm
from skimage.registration import phase_cross_correlation
from scipy.optimize import least_squares

sys.path.append(str(Path(__file__).resolve().parent))
from lib_shared import stitch_canvas, save_tiff

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
from basicpy import BaSiC

# ─── Paths ───────────────────────────────────────────────────────────────────
PROJECT_ROOT   = Path(__file__).resolve().parent.parent
WELL_DIR       = PROJECT_ROOT / "00.RawData" / "MouseTestRawdata20260421" / "A1-Image Export-01"
INTERMEDIATE   = PROJECT_ROOT / "intermediate" / "mouse" / "A1-Image Export-01"
RESULTS        = PROJECT_ROOT / "02.Results"   / "A1-Image Export-01"
RESULTS.mkdir(parents=True, exist_ok=True)

TILE_PX = 1020       # tile side length in pixels (constant for this scanner)
MAX_PHASE_SHIFT = 25 # maximum allowed sub-pixel correction per pair (px)
LAMBDA_ANCHOR   = 0.5  # Tikhonov regularisation weight

# ─── Step 1: Parse geometry from _meta.xml ─────────────────────────────────
def parse_meta(xml_path: Path):
    """Returns scale_m and list of scene dicts with grid geometry."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    scale_m = None
    for d in root.findall(".//Scaling/Items/Distance"):
        if d.get("Id") == "X":
            scale_m = float(d.findtext("Value"))
            break
    if scale_m is None:
        raise RuntimeError("No X-scale in _meta.xml")

    scenes = []
    for tr in root.findall(".//TileRegion"):
        cols = int(tr.findtext("Columns"))
        rows = int(tr.findtext("Rows"))
        size_w, size_h = [float(v) for v in tr.findtext("ContourSize").split(",")]
        step_x_px = (size_w / cols) / (scale_m * 1e6)
        step_y_px = (size_h / rows) / (scale_m * 1e6)
        scenes.append(dict(cols=cols, rows=rows,
                           step_x_px=step_x_px, step_y_px=step_y_px))
    return scale_m, scenes

# ─── Step 2: Build meander grid → only existing tiles ───────────────────────
def build_scene_tiles(well_dir: Path, scene_idx: int, cols: int, rows: int,
                      step_x_px: float, step_y_px: float):
    """
    Traverses the meander grid (row 0 L→R, row 1 R→L, …) assigning each grid
    cell a sequential m-index.  Only cells whose .tif file actually exists are
    kept; the m-index cursor advances regardless so the spatial assignment is
    correct.

    Returns list of tile dicts: filename, x_px, y_px, w, h.
    """
    s_id = scene_idx + 1  # ZEN filenames use 1-based scene index
    prefix = f"A1-Image Export-01"

    tiles = []
    m = 1   # meander sequential counter
    for row in range(rows):
        cols_range = range(cols) if row % 2 == 0 else range(cols - 1, -1, -1)
        for col in cols_range:
            fn = f"{prefix}_s{s_id}m{m:03d}_ORG.tif"
            fpath = well_dir / fn
            if fpath.exists():
                x_px = int(round(col * step_x_px))
                y_px = int(round(row * step_y_px))
                tiles.append(dict(filename=fn, x=float(x_px), y=float(y_px),
                                  w=TILE_PX, h=TILE_PX))
            m += 1
    return tiles

# ─── Step 3: BaSiCPy flatfield correction ───────────────────────────────────
def basicpy_correct(well_dir: Path, tiles: list, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    if all((out_dir / t["filename"]).exists() for t in tiles):
        print("      [BaSiCPy] Already corrected, skipping.")
        return

    np.random.seed(42)
    sample_size = min(len(tiles), 300)
    sample = np.random.choice(tiles, sample_size, replace=False)

    imgs = []
    for t in sample:
        p = well_dir / t["filename"]
        if p.exists():
            imgs.append(np.squeeze(tifffile.imread(str(p))))
    if not imgs:
        return

    print(f"      [BaSiCPy] Fitting on {len(imgs)} proxy tiles…")
    basic = BaSiC(get_darkfield=False, smoothness_flatfield=1.0)
    basic.fit(np.array(imgs))
    ff = basic.flatfield + 1e-6

    print(f"      [BaSiCPy] Applying flatfield to {len(tiles)} tiles…")
    for t in tqdm(tiles, desc="        Correcting", leave=False):
        ip = well_dir / t["filename"]
        op = out_dir  / t["filename"]
        if not ip.exists():
            continue
        raw = np.squeeze(tifffile.imread(str(ip)))
        corrected = np.clip(raw / ff, 0, 255).astype(np.uint8)
        tifffile.imwrite(str(op), corrected, compression="deflate")

# ─── Step 4: Build grid index map from tile list ────────────────────────────
def build_grid(tiles: list):
    """
    Derives discrete (row, col) grid index for each tile from its (y_px, x_px).
    Uses median step to handle float rounding.
    """
    xs = sorted(set(t["x"] for t in tiles))
    ys = sorted(set(t["y"] for t in tiles))

    step_x = float(np.median(np.diff(xs))) if len(xs) > 1 else TILE_PX
    step_y = float(np.median(np.diff(ys))) if len(ys) > 1 else TILE_PX
    min_x, min_y = xs[0], ys[0]

    grid = {}      # (row,col) -> tile dict
    idx_map = {}   # (row,col) -> integer index in `tiles`

    for i, t in enumerate(tiles):
        col = int(round((t["x"] - min_x) / step_x))
        row = int(round((t["y"] - min_y) / step_y))
        grid[(row, col)] = t
        idx_map[(row, col)] = i

    return grid, idx_map

# ─── Step 5: Bounded phase-correlation shift ────────────────────────────────
def bounded_phase_shift(img_a: np.ndarray, img_b: np.ndarray,
                        direction: str, overlap_x: int, overlap_y: int,
                        max_shift: int = MAX_PHASE_SHIFT):
    """
    Extracts the overlap strip between two adjacent tiles guided by the
    stage-coordinate nominal overlap, then refines the offset with
    phase_cross_correlation capped to max_shift px.
    """
    if overlap_x <= 0 or overlap_y <= 0:
        return 0.0, 0.0

    if direction == "horizontal":
        sa = img_a[:, -overlap_x:]
        sb = img_b[:,  :overlap_x]
    else:
        sa = img_a[-overlap_y:, :]
        sb = img_b[: overlap_y, :]

    # Limit strip height/width for memory
    MAX_D = 800
    if direction == "horizontal" and sa.shape[0] > MAX_D:
        mid = sa.shape[0] // 2; half = MAX_D // 2
        sa = sa[mid-half:mid+half, :]; sb = sb[mid-half:mid+half, :]
    elif direction == "vertical" and sa.shape[1] > MAX_D:
        mid = sa.shape[1] // 2; half = MAX_D // 2
        sa = sa[:, mid-half:mid+half]; sb = sb[:, mid-half:mid+half]

    try:
        shift, _, _ = phase_cross_correlation(
            sa, sb, normalization="phase", upsample_factor=10)
        dy, dx = float(shift[0]), float(shift[1])
        if abs(dy) > max_shift or abs(dx) > max_shift:
            return 0.0, 0.0
        return dy, dx
    except Exception:
        return 0.0, 0.0

# ─── Step 6: Globally consistent solver with Tikhonov anchor ────────────────
def solve_positions(grid: dict, idx_map: dict, tiles: list,
                    refined_shifts: dict):
    """
    Solves absolute positions minimising pairwise shift residuals while
    anchoring the solution to the stage-coordinate prior with a Tikhonov term.
    """
    n = len(tiles)
    init_pos = np.zeros((n, 2), dtype=np.float64)
    for (row, col), t in grid.items():
        i = idx_map[(row, col)]
        init_pos[i, 0] = t["y"]
        init_pos[i, 1] = t["x"]

    if not refined_shifts:
        print("      [Solver] No phase shifts — using raw stage coordinates.")
        return {t["filename"]: (t["y"], t["x"]) for t in tiles}

    def residuals(pos_flat):
        pos = pos_flat.reshape(n, 2)
        res = []
        for (ka, kb), (dy_ref, dx_ref) in refined_shifts.items():
            ia, ib = idx_map[ka], idx_map[kb]
            res.append(pos[ib, 0] - pos[ia, 0] - dy_ref)
            res.append(pos[ib, 1] - pos[ia, 1] - dx_ref)
        # Tikhonov anchor: keep solution close to stage coordinates
        drift = (pos - init_pos) * LAMBDA_ANCHOR
        res.extend(drift.flatten())
        return np.array(res)

    print("      [Solver] Running Tikhonov-anchored least-squares…")
    result = least_squares(residuals, init_pos.flatten(), method="lm", max_nfev=8000)
    opt = result.x.reshape(n, 2)
    positions = {}
    for (row, col), t in grid.items():
        i = idx_map[(row, col)]
        positions[t["filename"]] = (opt[i, 0], opt[i, 1])
    return positions

# ─── Pipeline: process one scene ────────────────────────────────────────────
def process_scene(scene_idx: int, scene_info: dict):
    s = scene_info
    cols, rows = s["cols"], s["rows"]
    step_x_px, step_y_px = s["step_x_px"], s["step_y_px"]

    print(f"\n  Scene {scene_idx} ({cols}×{rows} grid, step={step_x_px:.1f}×{step_y_px:.1f} px)")

    # ── Tile list (only existing files)
    tiles = build_scene_tiles(WELL_DIR, scene_idx, cols, rows, step_x_px, step_y_px)
    if not tiles:
        print(f"    No tiles found — skipping.")
        return
    print(f"    {len(tiles)} tiles found (grid {cols}×{rows}={cols*rows}, {cols*rows-len(tiles)} edge cells absent)")

    # ── BaSiCPy correction
    basic_dir = INTERMEDIATE / f"scene{scene_idx}" / "basic_corrected"
    basicpy_correct(WELL_DIR, tiles, basic_dir)

    # ── Output check
    out_path = RESULTS / f"scene{scene_idx}_optimal.tif"
    if out_path.exists():
        print(f"    {out_path.name} already exists — skipping stitching.")
        return

    # ── Build spatial grid index
    grid, idx_map = build_grid(tiles)

    # ── Enumerate adjacent pairs
    pairs = []
    for (row, col) in sorted(grid.keys()):
        if (row, col + 1) in grid:
            pairs.append(("horizontal", (row, col), (row, col + 1)))
        if (row + 1, col) in grid:
            pairs.append(("vertical", (row, col), (row + 1, col)))

    # ── Compute bounded phase shifts
    refined_shifts = {}
    for direction, ka, kb in tqdm(pairs, desc="      Phase corr", leave=False):
        pa = basic_dir / grid[ka]["filename"]
        pb = basic_dir / grid[kb]["filename"]
        if not pa.exists() or not pb.exists():
            continue
        a_geo, b_geo = grid[ka], grid[kb]
        # Nominal overlap derived from stage coordinates
        dx_nom = b_geo["x"] - a_geo["x"]
        dy_nom = b_geo["y"] - a_geo["y"]
        ov_x = max(0, int(TILE_PX - dx_nom)) if direction == "horizontal" else TILE_PX
        ov_y = max(0, int(TILE_PX - dy_nom)) if direction == "vertical"   else TILE_PX

        img_a = np.squeeze(tifffile.imread(str(pa))).astype(np.float32)
        img_b = np.squeeze(tifffile.imread(str(pb))).astype(np.float32)
        dy_corr, dx_corr = bounded_phase_shift(img_a, img_b, direction, ov_x, ov_y)
        # Store refined absolute displacements
        refined_shifts[(ka, kb)] = (dy_nom + dy_corr, dx_nom + dx_corr)

    # ── Global position optimisation
    positions = solve_positions(grid, idx_map, tiles, refined_shifts)

    # ── Stitch
    sample = np.squeeze(tifffile.imread(str(basic_dir / tiles[0]["filename"])))
    tile_h, tile_w = sample.shape
    print(f"    Stitching {len(tiles)} corrected tiles…")
    canvas = stitch_canvas(positions, basic_dir, tiles, tile_h, tile_w, downsample=1)
    if canvas is not None:
        save_tiff(canvas, out_path)
    else:
        print(f"    [WARN] stitch_canvas returned None for scene {scene_idx}")

# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    meta_xml = WELL_DIR / "A1-Image Export-01_meta.xml"
    if not meta_xml.exists():
        print("ERROR: _meta.xml not found.")
        sys.exit(1)

    scale_m, scenes = parse_meta(meta_xml)
    print(f"A1 — {len(scenes)} scenes, pixel scale {scale_m*1e6:.4f} µm/px")

    for i, info in enumerate(scenes):
        process_scene(i, info)

    print("\n✓ A1 stitching complete.")

if __name__ == "__main__":
    main()
