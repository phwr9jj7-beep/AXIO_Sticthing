"""
04_stitch_coordinate.py
-----------------------
Stitches tiles by using the EXACT stage coordinates from the Zeiss XML
(StartX, StartY) to place each tile on the canvas. This is the simplest,
most robust method — it requires no feature matching and is guaranteed to
produce a geometrically correct result as long as the stage calibration is
accurate (which it almost always is for modern Zeiss systems).

Approach:
  - Parse StartX / StartY from XML (in pixel units, already calibrated by ZEN)
  - Normalize coordinates so top-left tile is at (0, 0)
  - Place each tile on the output canvas with simple averaging blending in
    overlap regions (feathered linear blending is optionally enabled)
  - Output: one TIFF per scene

Usage:
    py -3 scripts/04_stitch_coordinate.py --dataset 0347 --source raw
    py -3 scripts/04_stitch_coordinate.py --dataset 0347 --source corrected
    py -3 scripts/04_stitch_coordinate.py --dataset all --source corrected
    py -3 scripts/04_stitch_coordinate.py --dataset 0347 --scene 0  (single scene)

Arguments:
    --dataset   : 0347, RecognizedCode, or all
    --source    : raw (original TIFs) or corrected (BaSiCPy output)
    --scene     : process only a specific scene index (optional)
    --blend     : blending mode: 'average' (default), 'linear_feather', 'overwrite'
    --downsample: integer downscale factor for output (default 1 = full res)
                  Use 4 or 8 to produce fast preview images.
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


def make_linear_weight(tile_h, tile_w, border_frac=0.15):
    """Create a weight mask with linear feathering at tile borders."""
    border_y = max(1, int(tile_h * border_frac))
    border_x = max(1, int(tile_w * border_frac))
    wy = np.ones(tile_h, dtype=np.float32)
    wx = np.ones(tile_w, dtype=np.float32)
    for i in range(border_y):
        v = (i + 1) / (border_y + 1)
        wy[i] = v
        wy[tile_h - 1 - i] = v
    for j in range(border_x):
        v = (j + 1) / (border_x + 1)
        wx[j] = v
        wx[tile_w - 1 - j] = v
    return np.outer(wy, wx)


def stitch_scene(scene_tiles, raw_dir, corrected_dir, out_path, source, blend, downsample):
    """Place all tiles for one scene onto a canvas using stage coordinates."""
    
    xs = [t["x"] for t in scene_tiles]
    ys = [t["y"] for t in scene_tiles]
    tile_w = scene_tiles[0]["w"]
    tile_h = scene_tiles[0]["h"]

    x_min, y_min = min(xs), min(ys)
    x_max, y_max = max(xs), max(ys)

    canvas_w = (x_max - x_min + tile_w) // downsample
    canvas_h = (y_max - y_min + tile_h) // downsample
    tile_w_ds = tile_w // downsample
    tile_h_ds = tile_h // downsample

    print(f"    Canvas size       : {canvas_w} x {canvas_h} px  (downsample={downsample}x)")
    print(f"    Tiles             : {len(scene_tiles)}")
    print(f"    Source            : {source}")

    # Use float32 accumulator + weight map for blending
    accumulator = np.zeros((canvas_h, canvas_w), dtype=np.float32)
    weight_map  = np.zeros((canvas_h, canvas_w), dtype=np.float32)

    if blend == "linear_feather":
        tile_weight = make_linear_weight(tile_h_ds, tile_w_ds)
    else:
        tile_weight = np.ones((tile_h_ds, tile_w_ds), dtype=np.float32)

    for t in tqdm(scene_tiles, desc="    Placing tiles", unit="tile"):
        tif_path = (corrected_dir / t["filename"]) if source == "corrected" else (raw_dir / t["filename"])
        if not tif_path.exists():
            print(f"\n    [WARN] Missing: {tif_path.name}")
            continue

        tile = tifffile.imread(str(tif_path)).astype(np.float32)

        # Downsample if needed
        if downsample > 1:
            from skimage.transform import downscale_local_mean
            factors = (downsample,) * tile.ndim
            tile = downscale_local_mean(tile, factors).astype(np.float32)
            # Trim to expected size (integer rounding)
            tile = tile[:tile_h_ds, :tile_w_ds]

        # Canvas placement
        cx = (t["x"] - x_min) // downsample
        cy = (t["y"] - y_min) // downsample

        # Clip bounds to canvas (should not be needed but guards edge tiles)
        th, tw = tile.shape
        cy2 = min(cy + th, canvas_h)
        cx2 = min(cx + tw, canvas_w)
        tile = tile[:cy2 - cy, :cx2 - cx]
        w   = tile_weight[:cy2 - cy, :cx2 - cx]

        if blend == "overwrite":
            accumulator[cy:cy2, cx:cx2] = tile
            weight_map[cy:cy2, cx:cx2]  = 1.0
        else:
            accumulator[cy:cy2, cx:cx2] += tile * w
            weight_map[cy:cy2, cx:cx2]  += w

    # Normalize by accumulated weights
    valid = weight_map > 0
    canvas = np.zeros_like(accumulator)
    canvas[valid] = accumulator[valid] / weight_map[valid]

    # Convert back to uint16
    canvas = np.clip(canvas, 0, 65535).astype(np.uint16)

    print(f"    Output range      : [{canvas.min()}, {canvas.max()}]")
    tifffile.imwrite(
        str(out_path), canvas,
        compression="deflate",
        photometric="minisblack",
    )
    print(f"    Saved → {out_path.name}  ({out_path.stat().st_size / 1024**2:.1f} MB)")


def run_stitching(dataset_name: str, config: dict, source: str, blend: str,
                  scene_filter: int | None, downsample: int):
    raw_dir = config["raw_dir"]
    corrected_dir = config["corrected_dir"]
    out_dir = RESULTS / dataset_name
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Coordinate Stitching: {dataset_name}")
    print(f"  Blend mode: {blend} | Downsample: {downsample}x")
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
            print(f"  [WARN] Scene {s} not found in XML, skipping.")
            continue
        print(f"\n  Processing Scene {s} ({len(scenes[s])} tiles)...")
        suffix = f"_ds{downsample}" if downsample > 1 else ""
        out_fn = f"{dataset_name}_scene{s}_{source}{suffix}_coord_stitch.tif"
        out_path = out_dir / out_fn
        stitch_scene(
            scene_tiles=scenes[s],
            raw_dir=raw_dir,
            corrected_dir=corrected_dir,
            out_path=out_path,
            source=source,
            blend=blend,
            downsample=downsample,
        )

    print(f"\n✓ Coordinate stitching complete: {dataset_name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=["0347", "RecognizedCode", "all"], default="all")
    parser.add_argument("--source", choices=["raw", "corrected"], default="corrected",
                        help="Tile source: raw originals or BaSiCPy-corrected")
    parser.add_argument("--scene", type=int, default=None, help="Process only this scene index")
    parser.add_argument("--blend", choices=["average", "linear_feather", "overwrite"],
                        default="linear_feather", help="Blending mode for overlaps")
    parser.add_argument("--downsample", type=int, default=1,
                        help="Downscale factor (1=full res, 4=preview, 8=thumbnail)")
    args = parser.parse_args()

    targets = list(DATASET_MAP.keys()) if args.dataset == "all" else [args.dataset]
    for name in targets:
        run_stitching(
            name, DATASET_MAP[name],
            source=args.source,
            blend=args.blend,
            scene_filter=args.scene,
            downsample=args.downsample,
        )


if __name__ == "__main__":
    main()
