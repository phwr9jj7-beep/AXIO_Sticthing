"""
lib_stitch_coord.py
-------------------
Stitching engine using nominal stage coordinates directly from the Zeiss XML.
Places each tile at its defined coordinates with linear feathered blending in overlaps.
"""

import numpy as np
from pathlib import Path
from lib_shared import stitch_canvas, save_tiff


def run_coord_stitch(source_dir: Path, scene_tiles: list, out_path: Path, downsample: int):
    """
    Stage Coordinate blending stitcher. Uses XML x/y positions directly.
    """
    if out_path.exists():
        return
        
    print(f"    [Coordinate Stitcher] -> {out_path.name}")
    
    # Base positions are just the original bounding positions
    positions = {}
    for t in scene_tiles:
        positions[t["filename"]] = (t["y"], t["x"])
        
    tile_w = scene_tiles[0]["w"]
    tile_h = scene_tiles[0]["h"]
    
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
