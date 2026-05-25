"""
lib_correct_median.py
---------------------
Calculates shading correction profile by computing the median intensity across all tiles,
smoothing the profile via a Gaussian filter, and applying it to each tile.
"""

import numpy as np
import tifffile
from pathlib import Path
from tqdm import tqdm
import scipy.ndimage


def run_median_correction(dataset_name: str, scene_id: int, config: dict):
    raw_dir = config["raw_dir"]
    out_dir = config["median_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Check if all tiles for this scene already exist in output
    scene_tiles = [t for t in config["tiles"] if t["scene"] == scene_id]
    if all((out_dir / t["filename"]).exists() for t in scene_tiles):
        return out_dir
        
    print(f"    [Median Correction] Processing {len(scene_tiles)} tiles ...")
    
    # 1. Load up to 300 tiles to calculate median
    sample_tiles = scene_tiles
    if len(sample_tiles) > 300:
        indices = np.linspace(0, len(sample_tiles) - 1, 300, dtype=int)
        sample_tiles = [sample_tiles[i] for i in indices]
        
    stack = []
    base_shape = None
    for t in tqdm(sample_tiles, desc="      Loading subset", leave=False):
        img = tifffile.imread(str(raw_dir / t["filename"])).astype(np.float32)
        if img.ndim > 2:
            img = np.squeeze(img)
            if img.ndim > 2:
                img = img[..., 0]
        if base_shape is None:
            base_shape = img.shape
        if img.shape == base_shape:
            stack.append(img)
            
    stack = np.stack(stack, axis=0)
    
    # 2. Compute median and smooth it
    print("      Computing median flatfield...")
    flatfield = np.nanmedian(stack, axis=0)
    del stack
    
    flatfield = scipy.ndimage.gaussian_filter(flatfield, sigma=50)
    ff_mean = flatfield.mean()
    
    # 3. Apply to all tiles
    for t in tqdm(scene_tiles, desc="      Correcting", leave=False):
        out_path = out_dir / t["filename"]
        if out_path.exists():
            continue
            
        raw = tifffile.imread(str(raw_dir / t["filename"])).astype(np.float32)
        if raw.ndim > 2:
            raw = np.squeeze(raw)
            if raw.ndim > 2:
                raw = raw[..., 0]
                
        # Division normalization (handle shape mismatch)
        th, tw = raw.shape
        fh, fw = flatfield.shape
        ff_crop = flatfield[:min(th, fh), :min(tw, fw)]
        
        corrected = raw.copy()
        corrected[:min(th, fh), :min(tw, fw)] = raw[:min(th, fh), :min(tw, fw)] / (ff_crop / ff_mean)
        
        original_max = np.iinfo(np.uint16).max
        corrected = np.clip(corrected, 0, original_max).astype(np.uint16)
        tifffile.imwrite(str(out_path), corrected, compression="deflate")
        
    return out_dir
