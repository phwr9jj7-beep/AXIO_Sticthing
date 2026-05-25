"""
05_stitch_phase_correlation.py
------------------------------
Stitches tiles using Phase Correlation (cross-power spectrum) to refine the
tile positions beyond the nominal stage coordinates. This is the gold-standard
"registration-based" approach.

Algorithm:
  1. Start from the XML stage coordinates as an initial grid.
  2. For each pair of adjacent tiles (horizontal/vertical neighbours), compute
     the sub-pixel displacement via phase correlation (FFT cross-power spectrum).
  3. Accept refined positions within a plausible range (±25% of the overlap).
  4. Solve a global least-squares position optimisation (similar to ASHLAR /
     Grid/Collection Stitching in Fiji) to find the globally consistent layout.
  5. Place tiles using linear feathered blending.

Output:
  intermediate/<dataset>/stitched/<dataset>_scene<N>_corrected_phase_stitch.tif

Usage:
    py -3 scripts/05_stitch_phase_correlation.py --dataset 0347 --scene 0
    py -3 scripts/05_stitch_phase_correlation.py --dataset 0347 --downsample 4
    py -3 scripts/05_stitch_phase_correlation.py --dataset all --downsample 1
"""

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict
import numpy as np
import tifffile
from tqdm import tqdm
from scipy.optimize import least_squares
from skimage.registration import phase_cross_correlation

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA = PROJECT_ROOT / "00.RawData"
INTERMEDIATE = PROJECT_ROOT / "intermediate"
RESULTS = PROJECT_ROOT / "01.Results"

DATASET_MAP = {
    "0347": {
        "raw_dir": RAW_DATA / "2026_04_17__18_55__0347",
        "xml": RAW_DATA / "2026_04_17__18_55__0347" / "2026_04_17__18_55__0347_info.xml",
        "corrected_dir": INTERMEDIATE / "0347" / "basic_corrected",
        "out_dir": INTERMEDIATE / "0347" / "stitched",
    },
    "RecognizedCode": {
        "raw_dir": RAW_DATA / "2026_04_17__RecognizedCode",
        "xml": RAW_DATA / "2026_04_17__RecognizedCode" / "2026_04_17__RecognizedCode_info.xml",
        "corrected_dir": INTERMEDIATE / "RecognizedCode" / "basic_corrected",
        "out_dir": INTERMEDIATE / "RecognizedCode" / "stitched",
    },
}

# Maximum shift allowed from nominal coordinate (fraction of tile size)
MAX_SHIFT_FRACTION = 0.25

# When building a neighbour graph, only sample this many overlap rows/cols
OVERLAP_SAMPLE_PX = 512


def load_tile(path: Path) -> np.ndarray:
    img = tifffile.imread(str(path)).astype(np.float32)
    return img


def compute_pairwise_shift(tile_a: np.ndarray, tile_b: np.ndarray,
                           direction: str, overlap_px: int) -> tuple[float, float]:
    """
    Estimate the displacement of tile_b relative to tile_a using phase
    cross-correlation on the overlapping strip.

    Returns (dy, dx) shift of tile_b relative to its nominal position.
    A positive dy means tile_b is lower than expected.
    """
    h, w = tile_a.shape
    if direction == "horizontal":  # tile_b is to the RIGHT of tile_a
        strip_a = tile_a[:, -overlap_px:]
        strip_b = tile_b[:, :overlap_px]
    else:  # direction == "vertical" — tile_b is BELOW tile_a
        strip_a = tile_a[-overlap_px:, :]
        strip_b = tile_b[:overlap_px, :]

    # Limit strip height/width to OVERLAP_SAMPLE_PX for speed
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
        shift, _, _ = phase_cross_correlation(strip_a, strip_b,
                                              normalization="phase",
                                              upsample_factor=10)
        return float(shift[0]), float(shift[1])
    except Exception:
        return 0.0, 0.0


def build_tile_grid(scene_tiles):
    """
    Build a dict mapping (row, col) → tile_info, given the XML coordinate list.
    Handles non-rectangular / irregular tile grids (meander scan).
    """
    xs = sorted(set(t["x"] for t in scene_tiles))
    ys = sorted(set(t["y"] for t in scene_tiles))
    x_to_col = {x: i for i, x in enumerate(xs)}
    y_to_row = {y: i for i, y in enumerate(ys)}
    grid = {}
    for t in scene_tiles:
        row = y_to_row[t["y"]]
        col = x_to_col[t["x"]]
        grid[(row, col)] = t
    return grid, len(ys), len(xs)


def global_position_optimisation(
    grid, n_rows, n_cols, tile_h, tile_w, refined_shifts
):
    """
    Solve a globally consistent set of tile positions based on pairwise shifts.
    Returns dict (row, col) -> (abs_y, abs_x) in pixels.
    """
    keys = sorted(grid.keys())
    idx = {k: i for i, k in enumerate(keys)}
    n = len(keys)

    # Initial guess from nominal grid
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
                # Nominal relative position
                nominal_dy = (rb - ra) * tile_h
                nominal_dx = (cb - ca) * tile_w
                # Measured position of b relative to a
                pred_dy = pos[ib, 0] - pos[ia, 0]
                pred_dx = pos[ib, 1] - pos[ia, 1]
                # Refined position of b relative to a
                refined_dy = nominal_dy + dy
                refined_dx = nominal_dx + dx
                res.extend([pred_dy - refined_dy, pred_dx - refined_dx])
        return np.array(res) if res else np.zeros(2)

    if not refined_shifts:
        # No pairwise constraints — fall back to nominal
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


def phase_stitch_scene(scene_tiles, source_dir, out_path, downsample,
                       max_pairs=200):
    """Run phase-correlation stitching for one scene."""
    tile_w = scene_tiles[0]["w"]
    tile_h = scene_tiles[0]["h"]
    overlap_x = int(tile_w * 0.1)
    overlap_y = int(tile_h * 0.1)
    max_shift_x = int(tile_w * MAX_SHIFT_FRACTION)
    max_shift_y = int(tile_h * MAX_SHIFT_FRACTION)

    grid, n_rows, n_cols = build_tile_grid(scene_tiles)
    print(f"    Grid layout       : {n_rows} rows x {n_cols} cols")

    # --- Compute pairwise shifts (subset for speed) ---
    pairs_h = []  # horizontal neighbours
    pairs_v = []  # vertical neighbours
    for (row, col) in sorted(grid.keys()):
        if (row, col + 1) in grid:
            pairs_h.append(((row, col), (row, col + 1)))
        if (row + 1, col) in grid:
            pairs_v.append(((row, col), (row + 1, col)))

    all_pairs = pairs_h + pairs_v
    # Use all pairs to ensure enough constraints for global optimization
    print(f"    Computing {len(all_pairs)} pairwise phase correlations...")

    refined_shifts = {}
    for (key_a, key_b) in tqdm(all_pairs, desc="    Phase corr", unit="pair"):
        t_a = grid[key_a]
        t_b = grid[key_b]

        path_a = source_dir / t_a["filename"]
        path_b = source_dir / t_b["filename"]
        if not path_a.exists() or not path_b.exists():
            continue

        img_a = load_tile(path_a)
        img_b = load_tile(path_b)

        ra, ca = key_a
        rb, cb = key_b
        direction = "horizontal" if cb > ca else "vertical"
        overlap_px = overlap_x if direction == "horizontal" else overlap_y

        dy, dx = compute_pairwise_shift(img_a, img_b, direction, overlap_px)

        # Reject implausible shifts (motion > max_shift_fraction of tile size)
        if abs(dy) > max_shift_y or abs(dx) > max_shift_x:
            dy, dx = 0.0, 0.0

        refined_shifts[(key_a, key_b)] = (dy, dx)

    # --- Global position optimisation ---
    print("    Running global position optimisation...")
    positions = global_position_optimisation(
        grid, n_rows, n_cols, tile_h, tile_w, refined_shifts
    )

    # --- Assemble canvas ---
    abs_ys = [v[0] for v in positions.values()]
    abs_xs = [v[1] for v in positions.values()]
    y_min, x_min = min(abs_ys), min(abs_xs)
    y_max, x_max = max(abs_ys), max(abs_xs)

    canvas_h_full = int(y_max - y_min + tile_h)
    canvas_w_full = int(x_max - x_min + tile_w)

    canvas_h = canvas_h_full // downsample
    canvas_w = canvas_w_full // downsample
    tile_h_ds = tile_h // downsample
    tile_w_ds = tile_w // downsample

    print(f"    Canvas size       : {canvas_w} x {canvas_h} px  (downsample={downsample}x)")

    accumulator = np.zeros((canvas_h, canvas_w), dtype=np.float32)
    weight_map  = np.zeros((canvas_h, canvas_w), dtype=np.float32)

    # Feathered weight mask
    border_frac = 0.12
    border_h = max(1, int(tile_h_ds * border_frac))
    border_w = max(1, int(tile_w_ds * border_frac))
    wy = np.ones(tile_h_ds, dtype=np.float32)
    wx = np.ones(tile_w_ds, dtype=np.float32)
    for i in range(border_h):
        v = (i + 1) / (border_h + 1)
        wy[i] = v; wy[tile_h_ds - 1 - i] = v
    for j in range(border_w):
        v = (j + 1) / (border_w + 1)
        wx[j] = v; wx[tile_w_ds - 1 - j] = v
    tile_weight = np.outer(wy, wx)

    for (row, col), (abs_y, abs_x) in tqdm(positions.items(), desc="    Assembling", unit="tile"):
        t = grid[(row, col)]
        tif_path = source_dir / t["filename"]
        if not tif_path.exists():
            continue

        tile = load_tile(tif_path)
        if downsample > 1:
            from skimage.transform import downscale_local_mean
            factors = (downsample,) * tile.ndim
            tile = downscale_local_mean(tile, factors).astype(np.float32)
            tile = tile[:tile_h_ds, :tile_w_ds]

        cy = int((abs_y - y_min) / downsample)
        cx = int((abs_x - x_min) / downsample)
        th, tw = tile.shape

        cy2 = min(cy + th, canvas_h)
        cx2 = min(cx + tw, canvas_w)
        tile = tile[:cy2 - cy, :cx2 - cx]
        w    = tile_weight[:cy2 - cy, :cx2 - cx]

        accumulator[cy:cy2, cx:cx2] += tile * w
        weight_map[cy:cy2, cx:cx2]  += w

    valid = weight_map > 0
    canvas = np.zeros_like(accumulator)
    canvas[valid] = accumulator[valid] / weight_map[valid]
    canvas = np.clip(canvas, 0, 65535).astype(np.uint16)

    print(f"    Output range      : [{canvas.min()}, {canvas.max()}]")
    tifffile.imwrite(str(out_path), canvas, compression="deflate", photometric="minisblack")
    print(f"    Saved → {out_path.name}  ({out_path.stat().st_size / 1024**2:.1f} MB)")


def run(dataset_name, config, source, scene_filter, downsample, max_pairs):
    xml_path = config["xml"]
    out_dir = config["out_dir"]
    corrected_dir = config["corrected_dir"]
    raw_dir = config["raw_dir"]
    source_dir = corrected_dir if source == "corrected" else raw_dir
    out_dir = RESULTS / dataset_name
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Phase-Correlation Stitching: {dataset_name}")
    print(f"{'='*60}")

    tree = ET.parse(xml_path)
    root = tree.getroot()
    scenes = defaultdict(list)
    for img in root.findall("Image"):
        fn = img.findtext("Filename")
        b = img.find("Bounds").attrib
        s = int(b.get("StartS", 0))
        scenes[s].append({
            "filename": fn,
            "x": int(b["StartX"]),
            "y": int(b["StartY"]),
            "w": int(b["SizeX"]),
            "h": int(b["SizeY"]),
        })

    target_scenes = [scene_filter] if scene_filter is not None else sorted(scenes.keys())
    for s in target_scenes:
        if s not in scenes:
            print(f"  [WARN] Scene {s} not found, skipping.")
            continue
        print(f"\n  Processing Scene {s} ({len(scenes[s])} tiles)...")
        suffix = f"_ds{downsample}" if downsample > 1 else ""
        out_fn = f"{dataset_name}_scene{s}_{source}{suffix}_phase_stitch.tif"
        phase_stitch_scene(
            scene_tiles=scenes[s],
            source_dir=source_dir,
            out_path=out_dir / out_fn,
            downsample=downsample,
            max_pairs=max_pairs,
        )
    print(f"\n✓ Phase-correlation stitching complete: {dataset_name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=["0347", "RecognizedCode", "all"], default="0347")
    parser.add_argument("--source", choices=["raw", "corrected"], default="corrected")
    parser.add_argument("--scene", type=int, default=None)
    parser.add_argument("--downsample", type=int, default=4,
                        help="Downscale factor (4 = quick preview, 1 = full res)")
    parser.add_argument("--max_pairs", type=int, default=200,
                        help="Max pairwise phase correlations to compute")
    args = parser.parse_args()

    targets = list(DATASET_MAP.keys()) if args.dataset == "all" else [args.dataset]
    for name in targets:
        run(name, DATASET_MAP[name], args.source, args.scene, args.downsample, args.max_pairs)


if __name__ == "__main__":
    main()
