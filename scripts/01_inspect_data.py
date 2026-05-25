"""
01_inspect_data.py
------------------
Parses the Zeiss AXIO XML info files and prints a summary of the tile grid
geometry for both datasets. Run this first to verify data integrity before
any processing.

Usage:
    py -3 scripts/01_inspect_data.py
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA = PROJECT_ROOT / "00.RawData"

DATASETS = {
    "0347": RAW_DATA / "2026_04_17__18_55__0347" / "2026_04_17__18_55__0347_info.xml",
    "RecognizedCode": RAW_DATA / "2026_04_17__RecognizedCode" / "2026_04_17__RecognizedCode_info.xml",
}


def inspect(name: str, xml_path: Path):
    print(f"\n{'='*60}")
    print(f"  Dataset : {name}")
    print(f"  XML     : {xml_path.name}")
    print(f"{'='*60}")

    tree = ET.parse(xml_path)
    root = tree.getroot()
    images = root.findall("Image")

    print(f"  Total tiles       : {len(images)}")

    bounds_list = []
    scenes = []
    for img in images:
        fn = img.findtext("Filename")
        b = img.find("Bounds").attrib
        sx = int(b["StartX"])
        sy = int(b["StartY"])
        w = int(b["SizeX"])
        h = int(b["SizeY"])
        scene = int(b.get("StartS", 0))
        bounds_list.append((sx, sy, w, h, scene, fn))
        scenes.append(scene)

    scene_counts = Counter(scenes)
    print(f"  Scenes            : {sorted(scene_counts.keys())} (tiles/scene: {dict(sorted(scene_counts.items()))})")

    # Per-scene grid summary
    for s in sorted(scene_counts.keys()):
        tiles = [(sx, sy, w, h, fn) for sx, sy, w, h, sc, fn in bounds_list if sc == s]
        xs = [t[0] for t in tiles]
        ys = [t[1] for t in tiles]
        w0, h0 = tiles[0][2], tiles[0][3]
        ux = sorted(set(xs))
        uy = sorted(set(ys))
        x_step = (ux[1] - ux[0]) if len(ux) > 1 else w0
        y_step = (uy[1] - uy[0]) if len(uy) > 1 else h0
        overlap_x = round((w0 - x_step) / w0 * 100, 1)
        overlap_y = round((h0 - y_step) / h0 * 100, 1)
        canvas_w = max(xs) - min(xs) + w0
        canvas_h = max(ys) - min(ys) + h0

        print(f"\n  --- Scene {s} ---")
        print(f"    Tile size         : {w0} x {h0} px")
        print(f"    Grid (col x row)  : {len(ux)} x {len(uy)}")
        print(f"    Step X / Y        : {x_step} / {y_step} px")
        print(f"    Overlap X / Y     : {overlap_x}% / {overlap_y}%")
        print(f"    Canvas (approx)   : {canvas_w} x {canvas_h} px")
        print(f"    X range           : {min(xs)} – {max(xs)}")
        print(f"    Y range           : {min(ys)} – {max(ys)}")

    # Verify all TIF files exist
    tif_dir = xml_path.parent
    missing = []
    for sx, sy, w, h, sc, fn in bounds_list:
        if not (tif_dir / fn).exists():
            missing.append(fn)
    if missing:
        print(f"\n  [WARNING] {len(missing)} TIF files listed in XML but NOT FOUND on disk!")
        for m in missing[:5]:
            print(f"    - {m}")
        if len(missing) > 5:
            print(f"    ... and {len(missing)-5} more")
    else:
        print(f"\n  [OK] All {len(images)} TIF files verified on disk.")


if __name__ == "__main__":
    for name, xml_path in DATASETS.items():
        if xml_path.exists():
            inspect(name, xml_path)
        else:
            print(f"\n[ERROR] XML not found: {xml_path}")
