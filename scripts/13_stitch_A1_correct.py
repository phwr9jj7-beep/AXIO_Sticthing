"""
13_stitch_A1_correct.py
-----------------------
Correct A1 stitching script.

The real A1 raw data is in:
  00.RawData/MouseTestRawdata20260421/A1/
  (NOT in "A1-Image Export-01/")

A1_info.xml is valid (3875 images, explicit StartX/StartY per tile).
This script uses the same BaSiCPy + Bounded-Phase-Correlation pipeline
as 11_mouse_stitch_optimal.py, but targets the correct folder.

Output: 02.Results/A1/scene{N}_optimal.tif
"""

import os
import sys
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

# ─── Paths ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WELL_DIR     = PROJECT_ROOT / "00.RawData" / "MouseTestRawdata20260421" / "A1"
INTERMEDIATE = PROJECT_ROOT / "intermediate" / "mouse" / "A1"
RESULTS      = PROJECT_ROOT / "02.Results" / "A1"
RESULTS.mkdir(parents=True, exist_ok=True)

TILE_PX       = 1020
MAX_PH_SHIFT  = 25       # px — discard phase shifts larger than this
LAMBDA_ANCHOR = 0.5      # Tikhonov regularisation weight

# ─── Step 1: Parse A1_info.xml (explicit StartX/StartY per tile) ───────────
def parse_info_xml(xml_path: Path):
    """Returns {scene_id: [dict(filename, x, y, w, h), ...]}"""
    tree = ET.parse(xml_path)
    images = tree.getroot().findall("Image")
    if not images:
        raise RuntimeError("Empty _info.xml")

    scenes = defaultdict(list)
    for img in images:
        fn = img.findtext("Filename")
        if not fn:
            continue
        b = img.find("Bounds")
        if b is None:
            continue
        a = b.attrib
        s = int(a.get("StartS", 0))
        scenes[s].append({
            "filename": fn,
            "x": float(a["StartX"]),
            "y": float(a["StartY"]),
            "w": int(a["SizeX"]),
            "h": int(a["SizeY"]),
        })
    return dict(scenes)

# ─── Step 2: Build (row, col) grid from absolute coordinates ──────────────
def build_grid(tiles: list):
    xs = sorted(set(t["x"] for t in tiles))
    ys = sorted(set(t["y"] for t in tiles))
    step_x = float(np.median(np.diff(xs))) if len(xs) > 1 else TILE_PX
    step_y = float(np.median(np.diff(ys))) if len(ys) > 1 else TILE_PX
    min_x, min_y = xs[0], ys[0]
    grid    = {}   # (row, col) -> tile dict
    idx_map = {}   # (row, col) -> integer index in `tiles`
    for i, t in enumerate(tiles):
        col = int(round((t["x"] - min_x) / step_x))
        row = int(round((t["y"] - min_y) / step_y))
        grid[(row, col)] = t
        idx_map[(row, col)] = i
    return grid, idx_map

# ─── Step 3: BaSiCPy flatfield correction ─────────────────────────────────
def basicpy_correct(tiles: list, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    if all((out_dir / t["filename"]).exists() for t in tiles):
        print("      [BaSiCPy] Already corrected — skipping.")
        return

    np.random.seed(42)
    sample = np.random.choice(tiles, min(len(tiles), 300), replace=False)
    imgs = []
    for t in sample:
        p = WELL_DIR / t["filename"]
        if p.exists():
            imgs.append(np.squeeze(tifffile.imread(str(p))))
    if not imgs:
        print("      [BaSiCPy] No tiles loaded — skipping correction.")
        return

    print(f"      [BaSiCPy] Fitting on {len(imgs)} tiles…")
    basic = BaSiC(get_darkfield=False, smoothness_flatfield=1.0)
    basic.fit(np.array(imgs))
    ff = basic.flatfield + 1e-6

    print(f"      [BaSiCPy] Applying to {len(tiles)} tiles…")
    for t in tqdm(tiles, desc="        Correcting", leave=False):
        ip = WELL_DIR  / t["filename"]
        op = out_dir   / t["filename"]
        if not ip.exists():
            continue
        raw = np.squeeze(tifffile.imread(str(ip))).astype(np.float32)
        corrected = np.clip(raw / ff, 0, 255).astype(np.uint8)
        tifffile.imwrite(str(op), corrected, compression="deflate")

# ─── Step 4: Bounded phase-correlation shift ──────────────────────────────
def bounded_phase_shift(img_a: np.ndarray, img_b: np.ndarray,
                        direction: str, ov_x: int, ov_y: int) -> tuple:
    """
    Extract the overlap strip determined by stage-coord geometry, then refine
    with phase cross-correlation. Any shift > MAX_PH_SHIFT px is rejected.
    """
    if ov_x <= 0 or ov_y <= 0:
        return 0.0, 0.0

    if direction == "horizontal":
        sa, sb = img_a[:, -ov_x:], img_b[:, :ov_x]
    else:
        sa, sb = img_a[-ov_y:, :], img_b[:ov_y, :]

    # Limit strip depth to keep memory reasonable
    MAX_D = 800
    if direction == "horizontal" and sa.shape[0] > MAX_D:
        mid, h = sa.shape[0] // 2, MAX_D // 2
        sa, sb = sa[mid-h:mid+h, :], sb[mid-h:mid+h, :]
    elif direction == "vertical" and sa.shape[1] > MAX_D:
        mid, h = sa.shape[1] // 2, MAX_D // 2
        sa, sb = sa[:, mid-h:mid+h], sb[:, mid-h:mid+h]

    try:
        shift, _, _ = phase_cross_correlation(
            sa, sb, normalization="phase", upsample_factor=10)
        dy, dx = float(shift[0]), float(shift[1])
        if abs(dy) > MAX_PH_SHIFT or abs(dx) > MAX_PH_SHIFT:
            return 0.0, 0.0
        return dy, dx
    except Exception:
        return 0.0, 0.0

# ─── Step 5: Tikhonov-anchored global position solver ─────────────────────
def solve_positions(grid: dict, idx_map: dict, tiles: list,
                    refined_shifts: dict) -> dict:
    n = len(tiles)
    init_pos = np.zeros((n, 2), dtype=np.float64)
    for (row, col), t in grid.items():
        i = idx_map[(row, col)]
        init_pos[i, 0] = t["y"]
        init_pos[i, 1] = t["x"]

    if not refined_shifts:
        print("      [Solver] No shifts — using raw stage coords.")
        return {t["filename"]: (t["y"], t["x"]) for t in tiles}

    def residuals(pos_flat):
        pos = pos_flat.reshape(n, 2)
        res = []
        for (ka, kb), (dy_ref, dx_ref) in refined_shifts.items():
            ia, ib = idx_map[ka], idx_map[kb]
            res.append(pos[ib, 0] - pos[ia, 0] - dy_ref)
            res.append(pos[ib, 1] - pos[ia, 1] - dx_ref)
        # Tikhonov anchor: pin solution to stage-coordinate prior
        drift = (pos - init_pos) * LAMBDA_ANCHOR
        res.extend(drift.flatten())
        return np.array(res)

    print("      [Solver] Running Tikhonov-anchored least-squares…")
    result = least_squares(residuals, init_pos.flatten(),
                           method="lm", max_nfev=8000)
    opt = result.x.reshape(n, 2)
    return {grid[(r, c)]["filename"]: (opt[idx_map[(r, c)], 0],
                                       opt[idx_map[(r, c)], 1])
            for (r, c) in grid}

# ─── Step 6: Process one scene ────────────────────────────────────────────
def process_scene(scene_idx: int, tiles: list):
    n = len(tiles)
    print(f"\n  Scene {scene_idx}: {n} tiles")

    basic_dir = INTERMEDIATE / f"scene{scene_idx}" / "basic_corrected"
    basicpy_correct(tiles, basic_dir)

    out_path = RESULTS / f"scene{scene_idx}_optimal.tif"
    if out_path.exists():
        print(f"    {out_path.name} already exists — skipping.")
        return

    grid, idx_map = build_grid(tiles)

    # Build adjacency pairs
    pairs = []
    for (r, c) in sorted(grid):
        if (r, c + 1) in grid: pairs.append(("horizontal", (r, c), (r, c + 1)))
        if (r + 1, c) in grid: pairs.append(("vertical",   (r, c), (r + 1, c)))

    # Bounded phase correlation for each pair
    refined_shifts = {}
    for direction, ka, kb in tqdm(pairs, desc="      Phase corr", leave=False):
        pa = basic_dir / grid[ka]["filename"]
        pb = basic_dir / grid[kb]["filename"]
        if not pa.exists() or not pb.exists():
            continue

        ag, bg = grid[ka], grid[kb]
        dx_nom = bg["x"] - ag["x"]
        dy_nom = bg["y"] - ag["y"]
        ov_x = max(0, int(TILE_PX - dx_nom)) if direction == "horizontal" else TILE_PX
        ov_y = max(0, int(TILE_PX - dy_nom)) if direction == "vertical"   else TILE_PX

        img_a = np.squeeze(tifffile.imread(str(pa))).astype(np.float32)
        img_b = np.squeeze(tifffile.imread(str(pb))).astype(np.float32)
        dy_c, dx_c = bounded_phase_shift(img_a, img_b, direction, ov_x, ov_y)
        refined_shifts[(ka, kb)] = (dy_nom + dy_c, dx_nom + dx_c)

    positions = solve_positions(grid, idx_map, tiles, refined_shifts)

    sample_img = np.squeeze(tifffile.imread(str(basic_dir / tiles[0]["filename"])))
    tile_h, tile_w = sample_img.shape

    print(f"    Stitching {n} tiles → {out_path.name}")
    canvas = stitch_canvas(positions, basic_dir, tiles, tile_h, tile_w, downsample=1)
    if canvas is not None:
        save_tiff(canvas, out_path)
        print(f"    ✓ Saved: {out_path}")
    else:
        print(f"    [WARN] stitch_canvas returned None for scene {scene_idx}")

# ─── Main ─────────────────────────────────────────────────────────────────
def main():
    xml_path = WELL_DIR / "A1_info.xml"
    if not xml_path.exists():
        print(f"ERROR: {xml_path} not found")
        sys.exit(1)

    print(f"Parsing {xml_path.name}…")
    scenes = parse_info_xml(xml_path)
    print(f"Found {len(scenes)} scenes: {sorted(scenes.keys())}")
    for s, tiles in sorted(scenes.items()):
        xs = len(set(t['x'] for t in tiles))
        ys = len(set(t['y'] for t in tiles))
        print(f"  Scene {s}: {len(tiles)} tiles ({xs} cols × {ys} rows)")

    for s_idx in sorted(scenes.keys()):
        process_scene(s_idx, scenes[s_idx])

    print("\n✓ A1 stitching complete.")
    print(f"  Results in: {RESULTS}")

if __name__ == "__main__":
    main()
