"""
run_benchmark.py
----------------
Benchmark script executing the 9-permutations stitching pipeline matrix
(shading corrections: none/basicpy/median/spatial x registrations: coord/phase/sift).
"""

import argparse
import sys
import subprocess
from pathlib import Path

# Auto-install dependency check
try:
    import cv2
    import networkx
except ImportError:
    print("Installing new dependencies for SIFT stitching (opencv-python, networkx)...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "opencv-python", "networkx"])

from lib_shared import parse_xml
from lib_correct_basicpy import run_basicpy_correction
from lib_correct_median import run_median_correction
from lib_correct_spatial import run_spatial_correction
from lib_stitch_coord import run_coord_stitch
from lib_stitch_phase import run_phase_stitch
from lib_stitch_sift import run_sift_stitch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA = PROJECT_ROOT / "00.RawData"
INTERMEDIATE = PROJECT_ROOT / "intermediate"
RESULTS = PROJECT_ROOT / "01.Results"

DATASET_CONFIGS = {
    "0347": {
        "raw_dir": RAW_DATA / "2026_04_17__18_55__0347",
        "xml": RAW_DATA / "2026_04_17__18_55__0347" / "2026_04_17__18_55__0347_info.xml",
        "basicpy_dir": INTERMEDIATE / "0347" / "basic_corrected",
        "median_dir": INTERMEDIATE / "0347" / "median_corrected",
        "spatial_dir": INTERMEDIATE / "0347" / "spatial_corrected",
    },
    "RecognizedCode": {
        "raw_dir": RAW_DATA / "2026_04_17__RecognizedCode",
        "xml": RAW_DATA / "2026_04_17__RecognizedCode" / "2026_04_17__RecognizedCode_info.xml",
        "basicpy_dir": INTERMEDIATE / "RecognizedCode" / "basic_corrected",
        "median_dir": INTERMEDIATE / "RecognizedCode" / "median_corrected",
        "spatial_dir": INTERMEDIATE / "RecognizedCode" / "spatial_corrected",
    },
}

CORRECTIONS = {
    "basicpy": run_basicpy_correction,
    "median": run_median_correction,
    "spatial": run_spatial_correction,
}

STITCHERS = {
    "coord": run_coord_stitch,
    "phase": run_phase_stitch,
    "sift": run_sift_stitch,
}


def run_benchmark(dataset_name: str, scene_filter: int, downsample: int):
    config = DATASET_CONFIGS[dataset_name]
    scenes_dict = parse_xml(config["xml"])
    
    # Store parsed tile array temporarily for correction stages to use
    all_tiles = []
    for s_id, t_list in scenes_dict.items():
        for t in t_list:
            t["scene"] = s_id
            all_tiles.append(t)
    config["tiles"] = all_tiles
    
    target_scenes = [scene_filter] if scene_filter is not None else sorted(scenes_dict.keys())
    
    print(f"\n{'='*60}")
    print(f"  AXIO Benchmark Runner: {dataset_name}")
    print(f"  Downsample : {downsample}x")
    print(f"{'='*60}")
    
    for scene_id in target_scenes:
        if scene_id not in scenes_dict:
            continue
            
        print(f"\n[{dataset_name}] --> SCENE {scene_id} <--")
        scene_tiles = scenes_dict[scene_id]
        
        # Make sure output directory exists
        out_dir = RESULTS / dataset_name
        out_dir.mkdir(parents=True, exist_ok=True)
        
        for corr_name, corr_fn in CORRECTIONS.items():
            print(f"\n  --- Correction: {corr_name} ---")
            tile_dir = corr_fn(dataset_name, scene_id, config)
            
            for stitch_name, stitch_fn in STITCHERS.items():
                suffix = f"_ds{downsample}" if downsample > 1 else ""
                out_fn = f"scene{scene_id}_{corr_name}_{stitch_name}{suffix}.tif"
                out_path = out_dir / out_fn
                
                stitch_fn(
                    source_dir=tile_dir,
                    scene_tiles=scene_tiles,
                    out_path=out_path,
                    downsample=downsample
                )

def main():
    parser = argparse.ArgumentParser(description="AXIO 3x3 Benchmark Pipeline")
    parser.add_argument("--dataset", choices=["0347", "RecognizedCode", "all"], default="0347")
    parser.add_argument("--scene", type=int, default=None)
    parser.add_argument("--downsample", type=int, default=1, 
                        help="Downscale factor (1=Full Res, 4=Preview)")
    args = parser.parse_args()

    targets = list(DATASET_CONFIGS.keys()) if args.dataset == "all" else [args.dataset]
    for name in targets:
        run_benchmark(name, args.scene, args.downsample)
        
    print("\n✓ Benchmark execution complete.")

if __name__ == "__main__":
    main()
