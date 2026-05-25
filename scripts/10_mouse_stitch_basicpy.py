"""
10_mouse_stitch_basicpy.py
--------------------------
Orchestrates flatfield shading correction (using BaSiCPy) and stage coordinate stitching
for multi-scene mouse brain tissue images. Reconstructs tile grid layouts from meander scan paths,
fits shading profiles, applies corrections, and stitches mosaics for both raw and corrected tiles.
"""

import os
import sys
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
import numpy as np
import tifffile
from tqdm import tqdm
from skimage.transform import downscale_local_mean

# Add local path to import shared lib
sys.path.append(str(Path(__file__).resolve().parent))
from lib_shared import stitch_canvas, save_tiff

# Hide jax warnings if basicpy uses it
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3" 
from basicpy import BaSiC

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "00.RawData" / "MouseTestRawdata20260421"
INTERMEDIATE_DIR = PROJECT_ROOT / "intermediate" / "mouse"
RESULTS_DIR = PROJECT_ROOT / "02.Results"

def parse_mouse_xml(xml_path: Path):
    """
    Parses the new mouse _meta.xml geometry.
    Returns: pixel scale (m/px) and a dict of scenes:
    {
      scene_idx_int: {
         "name": str,
         "cols": int,
         "rows": int,
         "step_y_px": float,
         "step_x_px": float
      }
    }
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    
    # Get pixel scale
    scale_m = None
    for d in root.findall('.//Scaling/Items/Distance'):
        if d.get('Id') == 'X':
            scale_m = float(d.findtext('Value'))
            break
            
    if scale_m is None:
        raise ValueError(f"Could not find pixel scaling in {xml_path}")
        
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
    """
    Returns dict {m_index_1_based: (abs_y_px, abs_x_px)}
    """
    positions = {}
    m = 1
    for row in range(rows):
        for col_idx in range(cols):
            # Odd rows (1, 3, ...) go right to left in a meander scan
            col = col_idx if row % 2 == 0 else (cols - 1 - col_idx)
            y = int(row * step_y_px)
            x = int(col * step_x_px)
            positions[m] = (y, x)
            m += 1
    return positions

def get_tile_list(well_dir: Path, scene_idx: int, n_tiles: int):
    """
    Returns list of dicts: {"filename": str, "m": int}
    Finds the correct prefix automatically inside well_dir.
    """
    # Sample file to find prefix: e.g. A1-Image Export-01_s1m001_ORG.tif
    # Note: scene_idx in filenames is 1-based usually
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

def correct_scene_basicpy(well_dir: Path, tile_list: list, out_dir: Path):
    """
    Runs BaSiCPy on the tile list from well_dir and saves to out_dir.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Check if already done
    if all((out_dir / t["filename"]).exists() for t in tile_list):
        print("      BaSiCPy correction already exists. Skipping compute.")
        return
        
    print(f"      Loading tiles for BaSiCPy fitting...")
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
    
    print(f"      Fitting BaSiCPy (get_darkfield=False) ...")
    basic = BaSiC(get_darkfield=False, smoothness_flatfield=1.0)
    basic.fit(images_for_fit)
    
    print(f"      Applying correction and saving {len(tile_list)} tiles...")
    for t in tqdm(tile_list, desc="        Correcting", leave=False):
        in_p = well_dir / t["filename"]
        out_p = out_dir / t["filename"]
        if not in_p.exists():
            continue
            
        raw = tifffile.imread(str(in_p))
        raw_sq = np.squeeze(raw)
        
        # Apply correction
        corrected = raw_sq / (basic.flatfield + 1e-6)
        
        # Restore to uint8 safely
        corrected = np.clip(corrected, 0, 255).astype(np.uint8)
        
        # Save
        tifffile.imwrite(str(out_p), corrected, compression="deflate")

def process_well(well_dir: Path):
    well_name = well_dir.name
    print(f"\\n{'='*60}\\n  Processing Well: {well_name}\\n{'='*60}")
    
    xml_path = well_dir / f"{well_name}_meta.xml"
    if not xml_path.exists():
        print(f"  [ERROR] Cannot find meta.xml in {well_dir}")
        return
        
    scale_m, scenes = parse_mouse_xml(xml_path)
    well_intermediate = INTERMEDIATE_DIR / well_name
    well_results = RESULTS_DIR / well_name
    well_results.mkdir(parents=True, exist_ok=True)
    
    for s_idx, s_info in scenes.items():
        cols, rows = s_info["cols"], s_info["rows"]
        n_tiles = cols * rows
        print(f"\\n  Scene {s_idx} ({s_info['name']}): {cols}x{rows} = {n_tiles} tiles")
        
        tile_list = get_tile_list(well_dir, s_idx, n_tiles)
        if not tile_list:
            print(f"    [WARN] No tile images found for Scene {s_idx}. Skipping.")
            continue
            
        meander_pos = compute_meander_positions(cols, rows, s_info["step_x_px"], s_info["step_y_px"])
        
        # 1. Correct tiles
        basic_dir = well_intermediate / f"scene{s_idx}" / "basic_corrected"
        correct_scene_basicpy(well_dir, tile_list, basic_dir)
        
        # Get tile dimensions from first tile
        sample_img = tifffile.imread(str(well_dir / tile_list[0]["filename"]))
        tile_h, tile_w = np.squeeze(sample_img).shape
        
        # Map filenames to positions
        positions = {}
        for t in tile_list:
            m = t["m"]
            positions[t["filename"]] = meander_pos[m]
            
        # 2. Stitch corrected
        out_basic = well_results / f"scene{s_idx}_basicpy_coord.tif"
        if not out_basic.exists():
            print(f"    Stitching Corrected (BaSiCPy)...")
            canvas_basic = stitch_canvas(positions, basic_dir, tile_list, tile_h, tile_w, downsample=1)
            if canvas_basic is not None:
                save_tiff(canvas_basic, out_basic)
        else:
            print(f"    {out_basic.name} already exists.")
            
        # 3. Stitch raw (for comparison)
        out_raw = well_results / f"scene{s_idx}_raw_coord.tif"
        if not out_raw.exists():
            print(f"    Stitching Raw...")
            canvas_raw = stitch_canvas(positions, well_dir, tile_list, tile_h, tile_w, downsample=1)
            if canvas_raw is not None:
                save_tiff(canvas_raw, out_raw)
        else:
            print(f"    {out_raw.name} already exists.")


def main():
    if not RAW_DATA_DIR.exists():
        print(f"RAW_DATA_DIR not found: {RAW_DATA_DIR}")
        sys.exit(1)
        
    well_dirs = sorted([d for d in RAW_DATA_DIR.iterdir() if d.is_dir()])
    print(f"Found {len(well_dirs)} well directories to process.")
    
    for well_dir in well_dirs:
        process_well(well_dir)
        
    print(f"\\n✓ Full Mouse Dataset Processing Complete.")

if __name__ == "__main__":
    main()
