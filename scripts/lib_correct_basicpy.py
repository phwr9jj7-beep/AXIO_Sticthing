"""
lib_correct_basicpy.py
----------------------
Illumination correction wrapper that references pre-computed BaSiCPy shading-corrected tiles.
"""

from pathlib import Path

def run_basicpy_correction(dataset_name: str, scene_id: int, config: dict):
    """
    Since BaSiCPy was already calculated (files in intermediate/<dataset>/basic_corrected/),
    this simply returns the path to those files.
    """
    out_dir = config["basicpy_dir"] 
    
    if not out_dir.exists():
        print(f"  [WARN] BaSiCPy directory missing: {out_dir}")
        
    return out_dir

