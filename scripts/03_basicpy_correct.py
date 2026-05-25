"""
03_basicpy_correct.py
---------------------
Applies BaSiCPy (Background and Shading Correction in Python) to all tiles
in a dataset, per scene. Outputs:
  - intermediate/<dataset>/basic_corrected/  : shading-corrected 16-bit TIFFs
  - intermediate/<dataset>/basic_corrected/QC_flatfield_scene<N>.tif
  - intermediate/<dataset>/basic_corrected/QC_darkfield_scene<N>.tif

Algorithm:
  BaSiCPy estimates the spatially varying illumination profile (flat-field)
  and dark-field from the tile stack itself, requiring no calibration slides.
  Reference: Peng et al. (2017) Nature Communications.

Usage:
    py -3 scripts/03_basicpy_correct.py --dataset 0347
    py -3 scripts/03_basicpy_correct.py --dataset RecognizedCode
    py -3 scripts/03_basicpy_correct.py --dataset all
"""

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict
import numpy as np
import tifffile
from tqdm import tqdm

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA = PROJECT_ROOT / "00.RawData"
INTERMEDIATE = PROJECT_ROOT / "intermediate"

DATASET_MAP = {
    "0347": {
        "raw_dir": RAW_DATA / "2026_04_17__18_55__0347",
        "xml": RAW_DATA / "2026_04_17__18_55__0347" / "2026_04_17__18_55__0347_info.xml",
        "out_dir": INTERMEDIATE / "0347" / "basic_corrected",
    },
    "RecognizedCode": {
        "raw_dir": RAW_DATA / "2026_04_17__RecognizedCode",
        "xml": RAW_DATA / "2026_04_17__RecognizedCode" / "2026_04_17__RecognizedCode_info.xml",
        "out_dir": INTERMEDIATE / "RecognizedCode" / "basic_corrected",
    },
}

# Maximum tiles to sample for BaSiCPy fitting per scene (for memory efficiency).
# Full stack is used if tile count <= this value.
MAX_SAMPLE_TILES = 300


def load_tiles_for_fitting(tile_paths, max_tiles=MAX_SAMPLE_TILES):
    """Load a representative subset of tiles for BaSiCPy fitting."""
    paths = list(tile_paths)
    if len(paths) > max_tiles:
        # Evenly spaced sampling for better spatial coverage
        indices = np.linspace(0, len(paths) - 1, max_tiles, dtype=int)
        paths = [paths[i] for i in indices]
    
    print(f"    Loading {len(paths)} tiles for BaSiCPy fitting...")
    stack = []
    for p in tqdm(paths, desc="    Loading", unit="tile", leave=False):
        img = tifffile.imread(p)
        stack.append(img.astype(np.float32))
    return np.stack(stack, axis=0)  # shape: (N, H, W)


def run_basicpy(dataset_name: str, config: dict):
    from basicpy import BaSiC

    raw_dir = config["raw_dir"]
    xml_path = config["xml"]
    out_dir = config["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  BaSiCPy Shading Correction: {dataset_name}")
    print(f"{'='*60}")

    # Parse XML — group tiles by scene
    tree = ET.parse(xml_path)
    root = tree.getroot()
    scenes = defaultdict(list)
    for img in root.findall("Image"):
        fn = img.findtext("Filename")
        b = img.find("Bounds").attrib
        scene_id = int(b.get("StartS", 0))
        scenes[scene_id].append({
            "filename": fn,
            "path": raw_dir / fn,
            "x": int(b["StartX"]),
            "y": int(b["StartY"]),
        })

    print(f"  Scenes found: {sorted(scenes.keys())} ({sum(len(v) for v in scenes.values())} total tiles)")

    for scene_id in sorted(scenes.keys()):
        tile_info = scenes[scene_id]
        tile_paths = [t["path"] for t in tile_info]
        n_tiles = len(tile_paths)
        print(f"\n  Scene {scene_id}: {n_tiles} tiles")

        # --- Step 1: Fit BaSiCPy model ---
        print("  Step 1: Fitting BaSiCPy model...")
        stack = load_tiles_for_fitting(tile_paths)
        # Use get_darkfield=False for fluorescence images unless autofluorescence is severe
        basic = BaSiC(get_darkfield=False, smoothness_flatfield=1.0)
        basic.fit(stack)

        # Save QC images (flat-field and dark-field profiles)
        ff = basic.flatfield
        df = basic.darkfield
        ff_path = out_dir / f"QC_flatfield_scene{scene_id}.tif"
        df_path = out_dir / f"QC_darkfield_scene{scene_id}.tif"
        tifffile.imwrite(str(ff_path), ff.astype(np.float32))
        tifffile.imwrite(str(df_path), df.astype(np.float32))
        print(f"    Flat-field range  : [{ff.min():.4f}, {ff.max():.4f}]  → {ff_path.name}")
        print(f"    Dark-field range  : [{df.min():.4f}, {df.max():.4f}]  → {df_path.name}")

        del stack  # free memory before applying correction

        # --- Step 2: Apply correction to ALL tiles in scene ---
        print(f"  Step 2: Applying correction to {n_tiles} tiles...")
        for t in tqdm(tile_info, desc=f"    Scene {scene_id} correction", unit="tile"):
            out_path = out_dir / t["filename"]
            if out_path.exists():
                continue  # skip already processed

            raw = tifffile.imread(t["path"]).astype(np.float32)
            # BaSiCPy correction: (raw - darkfield) / flatfield
            corrected = (raw - df) / (ff + 1e-6)
            # Clip and convert back to uint16
            original_max = np.iinfo(np.uint16).max
            corrected = np.clip(corrected, 0, original_max).astype(np.uint16)
            # Use deflate (zlib) compression — no imagecodecs needed
            tifffile.imwrite(str(out_path), corrected, compression="deflate")

        print(f"  Scene {scene_id} done → {out_dir}")

    print(f"\n✓ BaSiCPy correction complete for dataset: {dataset_name}")


def main():
    parser = argparse.ArgumentParser(description="BaSiCPy shading correction for Zeiss AXIO tiles")
    parser.add_argument("--dataset", choices=["0347", "RecognizedCode", "all"], default="all",
                        help="Which dataset to process (default: all)")
    args = parser.parse_args()

    targets = list(DATASET_MAP.keys()) if args.dataset == "all" else [args.dataset]
    for name in targets:
        run_basicpy(name, DATASET_MAP[name])


if __name__ == "__main__":
    main()
