"""
lib_correct_spatial.py
----------------------
Calculates shading correction profile by applying a rolling-ball/background subtraction
spatial filter to estimate local background illumination independent of biological details.
"""

import numpy as np
import tifffile
from pathlib import Path
from tqdm import tqdm
import scipy.ndimage


def run_spatial_correction(dataset_name: str, scene_id: int, config: dict):
    raw_dir = config["raw_dir"]
    out_dir = config["spatial_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    
    scene_tiles = [t for t in config["tiles"] if t["scene"] == scene_id]
    if all((out_dir / t["filename"]).exists() for t in scene_tiles):
        return out_dir
        
    print(f"    [Spatial Correction] Processing {len(scene_tiles)} tiles ...")
    
    for t in tqdm(scene_tiles, desc="      Correcting", leave=False):
        out_path = out_dir / t["filename"]
        if out_path.exists():
            continue
            
        raw = tifffile.imread(str(raw_dir / t["filename"])).astype(np.float32)
        if raw.ndim > 2:
            raw = np.squeeze(raw)
            if raw.ndim > 2:
                raw = raw[..., 0]
                
        # 1. Rolling ball background subtraction approximation
        # Tile sizes are either 1024 or 2048 roughly. 
        # Radius ~ 1/8 tile size.
        h, w = raw.shape
        ball_r = min(h, w) // 8
        
        # Grey erosion finds the minimum in the neighborhood
        bg = scipy.ndimage.grey_erosion(raw, size=(ball_r, ball_r))
        
        # Smooth the background
        bg = scipy.ndimage.gaussian_filter(bg, sigma=ball_r // 2)
        bg_median = np.median(bg)
        
        # 2. Subtract background and add global median 
        corrected = raw - bg + bg_median
        
        original_max = np.iinfo(np.uint16).max
        corrected = np.clip(corrected, 0, original_max).astype(np.uint16)
        tifffile.imwrite(str(out_path), corrected, compression="deflate")
        
    return out_dir
