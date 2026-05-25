"""
11_mouse_stitch_optimal.py
--------------------------
Calculates high-fidelity tile alignments for the mouse brain datasets.
Combines BaSiCPy shading correction, sub-pixel phase correlation displacement estimations,
and a global least-squares optimization incorporating a Tikhonov regularizer anchoring
the mosaic origin to stage limits to prevent translation invariance drift.
"""

import os
import sys
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict
import numpy as np
import tifffile
from tqdm import tqdm
from scipy.optimize import least_squares
from skimage.transform import downscale_local_mean
from skimage.registration import phase_cross_correlation

sys.path.append(str(Path(__file__).resolve().parent))
from lib_shared import stitch_canvas, save_tiff

# Hide TF/JAX warnings
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
from basicpy import BaSiC

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "00.RawData" / "MouseTestRawdata20260421"
INTERMEDIATE_DIR = PROJECT_ROOT / "intermediate" / "mouse"
RESULTS_DIR = PROJECT_ROOT / "02.Results"

def parse_info_xml(xml_path: Path):
    """
    Extracts explicit Stage Coordinates from the ZEN _info.xml.
    Returns: dict {scene_id: [{"filename": str, "x": float, "y": float, "w": int, "h": int}, ...]}
    """
    if not xml_path.exists():
        return {}
        
    tree = ET.parse(xml_path)
    images = tree.getroot().findall("Image")
    if not images:
        return {}
        
    scenes = defaultdict(list)
    for img in images:
        fn = img.findtext("Filename")
        if not fn:
            continue
        # Check if URL encoding is present in actual tile filenames
        fn = fn.replace("%20", " ") 
        
        b = img.find("Bounds")
        if b is None:
            continue
            
        attrib = b.attrib
        s = int(attrib.get("StartS", 0))
        scenes[s].append({
            "filename": fn,
            "x": float(attrib["StartX"]),
            "y": float(attrib["StartY"]),
            "w": int(attrib["SizeX"]),
            "h": int(attrib["SizeY"])
        })
    return dict(scenes)


def parse_meta_xml(xml_path: Path):
    """Fallback parser for when _info.xml is empty (like A1)."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    scale_m = None
    for d in root.findall('.//Scaling/Items/Distance'):
        if d.get('Id') == 'X':
            scale_m = float(d.findtext('Value'))
            break
            
    if scale_m is None:
        return None, {}
        
    scenes = {}
    for i, tr in enumerate(root.findall('.//TileRegion')):
        name = tr.get('Name')
        cols = int(tr.findtext('Columns'))
        rows = int(tr.findtext('Rows'))
        size_w, size_h = [float(v) for v in tr.findtext('ContourSize').split(',')]
        
        step_x_um = size_w / cols
        step_y_um = size_h / rows
        step_x_px = step_x_um / (scale_m * 1e6)
        step_y_px = step_y_um / (scale_m * 1e6)
        
        scenes[i] = {
            "name": name,
            "cols": cols,
            "rows": rows,
            "step_x_px": step_x_px,
            "step_y_px": step_y_px
        }
    return scale_m, scenes


def compute_meander_positions(cols, rows, step_x_px, step_y_px):
    positions = {}
    m = 1
    for row in range(rows):
        for col_idx in range(cols):
            col = col_idx if row % 2 == 0 else (cols - 1 - col_idx)
            positions[m] = (int(row * step_y_px), int(col * step_x_px))
            m += 1
    return positions


def get_tile_list_meta(well_dir: Path, scene_idx: int, n_tiles: int):
    s_id = scene_idx + 1 
    sample = list(well_dir.glob(f"*_s{s_id}m*_ORG.tif"))
    if not sample:
        return []
    prefix = sample[0].name.split(f"_s{s_id}m")[0]
    
    tiles = []
    for m in range(1, n_tiles + 1):
        fn = f"{prefix}_s{s_id}m{m:03d}_ORG.tif"
        tiles.append({"filename": fn, "m": m})
    return tiles


def construct_grid(scene_tiles: list):
    """
    Assigns each tile to a discrete (row, col) grid based on coordinates.
    """
    xs = sorted(list(set(t["x"] for t in scene_tiles)))
    ys = sorted(list(set(t["y"] for t in scene_tiles)))
    
    if len(xs) > 1:
        step_x = np.median(np.diff(xs))
    else:
        step_x = scene_tiles[0]["w"]
        
    if len(ys) > 1:
        step_y = np.median(np.diff(ys))
    else:
        step_y = scene_tiles[0]["h"]
        
    min_x = min(xs)
    min_y = min(ys)
    
    grid = {}
    idx_map = {}
    
    for i, t in enumerate(scene_tiles):
        col = int(round((t["x"] - min_x) / step_x))
        row = int(round((t["y"] - min_y) / step_y))
        grid[(row, col)] = t
        idx_map[(row, col)] = i
        
    return grid, idx_map


def compute_bounded_shift(img_a: np.ndarray, img_b: np.ndarray, direction: str, 
                          overlap_x: int, overlap_y: int, max_shift: int = 25):
    if overlap_x <= 0 or overlap_y <= 0:
        return 0.0, 0.0

    if direction == "horizontal":
        strip_a = img_a[:, -overlap_x:]
        strip_b = img_b[:, :overlap_x]
    else: 
        strip_a = img_a[-overlap_y:, :]
        strip_b = img_b[:overlap_y, :]
        
    MAX_DEPTH = 1000
    if direction == "horizontal" and strip_a.shape[0] > MAX_DEPTH:
        mid = strip_a.shape[0] // 2
        half = MAX_DEPTH // 2
        strip_a = strip_a[mid-half:mid+half, :]
        strip_b = strip_b[mid-half:mid+half, :]
    elif direction == "vertical" and strip_a.shape[1] > MAX_DEPTH:
        mid = strip_a.shape[1] // 2
        half = MAX_DEPTH // 2
        strip_a = strip_a[:, mid-half:mid+half]
        strip_b = strip_b[:, mid-half:mid+half]

    try:
        shift, _, _ = phase_cross_correlation(strip_a, strip_b, normalization="phase", upsample_factor=10)
        dy, dx = float(shift[0]), float(shift[1])
        if abs(dy) > max_shift or abs(dx) > max_shift:
            return 0.0, 0.0
        return dy, dx
    except Exception:
        return 0.0, 0.0


def solve_optimal_positions(grid: dict, idx_map: dict, scene_tiles: list, refined_shifts: dict):
    n = len(scene_tiles)
    init_pos = np.zeros((n, 2), dtype=np.float64)
    
    for (row, col), t in grid.items():
        i = idx_map[(row, col)]
        init_pos[i, 0] = t["y"]
        init_pos[i, 1] = t["x"]
        
    if not refined_shifts:
        print("        [PhaseCorr] No valid shifts found, using unmodified Stage Coordinates.")
        positions = {}
        for (row, col), t in grid.items():
            positions[t["filename"]] = (t["y"], t["x"])
        return positions

    def residuals(pos_flat):
        pos = pos_flat.reshape(n, 2)
        res = []
        for (a, b), (dy_ref, dx_ref) in refined_shifts.items():
            ia, ib = idx_map[a], idx_map[b]
            pred_dy = pos[ib, 0] - pos[ia, 0]
            pred_dx = pos[ib, 1] - pos[ia, 1]
            res.extend([pred_dy - dy_ref, pred_dx - dx_ref])
            
        # ANCHOR: Add absolute penalties so the entire canvas stays bound to the motor limits!
        # This acts as a Tikhonov regularizer strictly anchoring the mosaic origin.
        lambda_anchor = 0.5
        drift = (pos - init_pos) * lambda_anchor
        res.extend(drift.flatten())
        
        return np.array(res)

    print("        [Solver] Optimizing global mosaicking residuals...")
    result = least_squares(residuals, init_pos.flatten(), method="lm", max_nfev=5000)
    opt_pos = result.x.reshape(n, 2)
    
    positions = {}
    for (row, col), t in grid.items():
        i = idx_map[(row, col)]
        positions[t["filename"]] = (opt_pos[i, 0], opt_pos[i, 1])
        
    return positions


def correct_scene_basicpy(well_dir: Path, tile_list: list, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    if all((out_dir / t["filename"]).exists() for t in tile_list):
        return
        
    print(f"      Fitting BaSiCPy flatfield on {min(len(tile_list), 300)} proxy tiles...")
    np.random.seed(42)
    sample_size = min(len(tile_list), 300)
    sample_tiles = np.random.choice(tile_list, sample_size, replace=False)
    
    images_for_fit = []
    for t in sample_tiles:
        p = well_dir / t["filename"]
        if p.exists():
            img = tifffile.imread(str(p))
            images_for_fit.append(np.squeeze(img))
            
    if not images_for_fit:
        return
        
    images_for_fit = np.array(images_for_fit)
    basic = BaSiC(get_darkfield=False, smoothness_flatfield=1.0)
    basic.fit(images_for_fit)
    
    print(f"      Applying flatfield to {len(tile_list)} tiles...")
    flatfield = basic.flatfield + 1e-6
    for t in tqdm(tile_list, desc="        Correcting", leave=False):
        in_p = well_dir / t["filename"]
        out_p = out_dir / t["filename"]
        if not in_p.exists():
            continue
            
        raw = tifffile.imread(str(in_p))
        raw_sq = np.squeeze(raw)
        corrected = raw_sq / flatfield
        corrected = np.clip(corrected, 0, 255).astype(np.uint8)
        tifffile.imwrite(str(out_p), corrected, compression="deflate")


def process_well(well_dir: Path):
    well_name = well_dir.name
    
    # We enforce processing A1 specifically to complete the missed workload efficiently
    if well_name != "A1-Image Export-01":
        # Skip other wells since they were already done. (Or you can allow them, since out_optimal.exists() check is there).
        pass

    print(f"\\n{'='*60}\\n  Processing Well: {well_name}\\n{'='*60}")
    
    info_xmls = list(well_dir.glob("*_info.xml"))
    scenes = {}
    if info_xmls:
        scenes = parse_info_xml(info_xmls[0])
        
    # FALLBACK to _meta.xml
    if not scenes:
        print(f"  [WARN] {well_name}_info.xml is empty! Falling back to _meta.xml meander sequence.")
        meta_xml = well_dir / f"{well_name}_meta.xml"
        if meta_xml.exists():
            scale_m, meta_scenes = parse_meta_xml(meta_xml)
            for s_idx, s_info in meta_scenes.items():
                cols, rows = s_info["cols"], s_info["rows"]
                tile_list = get_tile_list_meta(well_dir, s_idx, cols * rows)
                meander_pos = compute_meander_positions(cols, rows, s_info["step_x_px"], s_info["step_y_px"])
                
                scenes[s_idx] = []
                for t in tile_list:
                    m = t["m"]
                    y_px, x_px = meander_pos[m]
                    scenes[s_idx].append({
                        "filename": t["filename"],
                        "x": float(x_px),
                        "y": float(y_px),
                        "w": 1020,
                        "h": 1020
                    })

    if not scenes:
        print(f"  [ERROR] {well_name} is corrupted. Both info and meta xml failed. Skipping.")
        return
        
    well_intermediate = INTERMEDIATE_DIR / well_name
    well_results = RESULTS_DIR / well_name
    well_results.mkdir(parents=True, exist_ok=True)
    
    for s_idx, scene_tiles in scenes.items():
        n_tiles = len(scene_tiles)
        print(f"\\n  Scene {s_idx}: extracting {n_tiles} tiles...")
        
        # 1. Correct Tiles
        basic_dir = well_intermediate / f"scene{s_idx}" / "basic_corrected"
        # We process BaSiCPy for A1. (A2 already has it generated before stitching).
        correct_scene_basicpy(well_dir, scene_tiles, basic_dir)
        
        out_optimal = well_results / f"scene{s_idx}_optimal.tif"
        if out_optimal.exists():
            print(f"    {out_optimal.name} already successfully stitched.")
            continue
            
        print("    Executing Bounded Phase Corr + Stage Coordinates...")
        grid, idx_map = construct_grid(scene_tiles)
        
        pairs_h, pairs_v = [], []
        for (row, col) in sorted(grid.keys()):
            if (row, col + 1) in grid: pairs_h.append(((row, col), (row, col + 1)))
            if (row + 1, col) in grid: pairs_v.append(((row, col), (row + 1, col)))
            
        all_pairs = pairs_h + pairs_v
        refined_shifts = {}
        
        for (key_a, key_b) in tqdm(all_pairs, desc="        Phase Intersect", leave=False):
            pa = basic_dir / grid[key_a]["filename"]
            pb = basic_dir / grid[key_b]["filename"]
            
            if not pa.exists() or not pb.exists():
                continue
                
            a_geo = grid[key_a]
            b_geo = grid[key_b]
            
            dx_exp = b_geo["x"] - a_geo["x"]
            dy_exp = b_geo["y"] - a_geo["y"]
            
            overlap_x = int(a_geo["w"] - dx_exp) if key_b[1] > key_a[1] else a_geo["w"]
            overlap_y = int(a_geo["h"] - dy_exp) if key_b[0] > key_a[0] else a_geo["h"]
            
            img_a = tifffile.imread(str(pa))
            img_b = tifffile.imread(str(pb))
            
            direction = "horizontal" if key_b[1] > key_a[1] else "vertical"
            shift_y, shift_x = compute_bounded_shift(img_a, img_b, direction, overlap_x, overlap_y)
            
            refined_shifts[(key_a, key_b)] = (dy_exp + shift_y, dx_exp + shift_x)
            
        positions = solve_optimal_positions(grid, idx_map, scene_tiles, refined_shifts)
        
        sample_img = tifffile.imread(str(basic_dir / scene_tiles[0]["filename"]))
        tile_h, tile_w = sample_img.shape
        
        print("    Stitching Optimized Canvas...")
        canvas_optimal = stitch_canvas(positions, basic_dir, scene_tiles, tile_h, tile_w, downsample=1)
        if canvas_optimal is not None:
            save_tiff(canvas_optimal, out_optimal)


def main():
    if not RAW_DATA_DIR.exists():
        print(f"RAW_DATA_DIR missing: {RAW_DATA_DIR}")
        sys.exit(1)
        
    well_dirs = sorted([d for d in RAW_DATA_DIR.iterdir() if d.is_dir()])
    print(f"Found {len(well_dirs)} well directories to process. Active solver: PhaseBounded+StageCoord")
    
    for well_dir in well_dirs:
        process_well(well_dir)
        
    print(f"\\n✓ Full Mouse Dataset Optimization Complete.")

if __name__ == "__main__":
    main()
