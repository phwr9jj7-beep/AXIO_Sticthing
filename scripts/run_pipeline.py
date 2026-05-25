"""
RUN_PIPELINE.bat / run_pipeline.py
-----------------------------------
Master pipeline runner. Executes all stages in order for a given dataset.

Usage:
    py -3 scripts/run_pipeline.py --dataset 0347 --scene 0 --preview
    py -3 scripts/run_pipeline.py --dataset RecognizedCode --full

Arguments:
    --dataset : 0347, RecognizedCode, or all
    --scene   : restrict to one scene (optional; default = all scenes)
    --preview : fast mode — downsample 4x, skip full-res output
    --full    : full-resolution mode (slow, large files)
    --skip_basic   : skip BaSiCPy correction (use raw tiles)
    --skip_coord   : skip coordinate-based stitching
    --skip_phase   : skip phase-correlation stitching
    --skip_qc      : skip QC report generation

Pipeline stages:
    1. inspect    → 01_inspect_data.py
    2. install    → 02_install_deps.py  (only first run)
    3. basicpy    → 03_basicpy_correct.py
    4. coord      → 04_stitch_coordinate.py
    5. phase      → 05_stitch_phase_correlation.py
    6. qc         → 06_qc_compare.py
"""

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PY = sys.executable


def run(args_list, desc=""):
    cmd = [PY] + [str(a) for a in args_list]
    print(f"\n{'─'*60}")
    print(f"  STAGE: {desc}")
    print(f"  CMD  : {' '.join(cmd)}")
    print(f"{'─'*60}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n[ERROR] Stage '{desc}' failed (exit {result.returncode}). Stopping pipeline.")
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="AXIO Stitching Pipeline")
    parser.add_argument("--dataset", choices=["0347", "RecognizedCode", "all"],
                        default="0347", help="Dataset to process")
    parser.add_argument("--scene", type=int, default=None,
                        help="Restrict to one scene index")
    parser.add_argument("--preview", action="store_true",
                        help="Fast preview mode (downsample 4x)")
    parser.add_argument("--full", action="store_true",
                        help="Full-resolution mode (downsample 1x)")
    parser.add_argument("--skip_install", action="store_true",
                        help="Skip dependency installation check")
    parser.add_argument("--skip_basic", action="store_true",
                        help="Skip BaSiCPy shading correction")
    parser.add_argument("--skip_coord", action="store_true",
                        help="Skip coordinate-based stitching")
    parser.add_argument("--skip_phase", action="store_true",
                        help="Skip phase-correlation stitching")
    parser.add_argument("--skip_qc", action="store_true",
                        help="Skip QC report generation")
    args = parser.parse_args()

    downsample = 1 if args.full else (4 if args.preview else 4)
    source = "corrected" if not args.skip_basic else "raw"
    scene_args = ["--scene", str(args.scene)] if args.scene is not None else []
    qc_scene = args.scene if args.scene is not None else 0

    print(f"\n{'='*60}")
    print(f"  AXIO Stitching Pipeline")
    print(f"  Dataset    : {args.dataset}")
    print(f"  Scene      : {'all' if args.scene is None else args.scene}")
    print(f"  Downsample : {downsample}x {'(preview)' if downsample > 1 else '(full-res)'}")
    print(f"  Source     : {source} tiles")
    print(f"{'='*60}")

    # Stage 1: Inspect
    run([SCRIPTS_DIR / "01_inspect_data.py"], desc="Data Inspection")

    # Stage 2: Install deps
    if not args.skip_install:
        run([SCRIPTS_DIR / "02_install_deps.py"], desc="Dependency Check")

    # Stage 3: BaSiCPy shading correction
    if not args.skip_basic:
        run([SCRIPTS_DIR / "03_basicpy_correct.py",
             "--dataset", args.dataset],
            desc="BaSiCPy Shading Correction")

    # Stage 4: Coordinate stitching
    if not args.skip_coord:
        run([SCRIPTS_DIR / "04_stitch_coordinate.py",
             "--dataset", args.dataset,
             "--source", source,
             "--blend", "linear_feather",
             "--downsample", str(downsample),
             ] + scene_args,
            desc="Coordinate-Based Stitching")

    # Stage 5: Phase-correlation stitching
    if not args.skip_phase:
        run([SCRIPTS_DIR / "05_stitch_phase_correlation.py",
             "--dataset", args.dataset,
             "--source", source,
             "--downsample", str(downsample),
             ] + scene_args,
            desc="Phase-Correlation Stitching")

    # Stage 6: QC comparison
    if not args.skip_qc:
        run([SCRIPTS_DIR / "06_qc_compare.py",
             "--dataset", args.dataset,
             "--scene", str(qc_scene)],
            desc="QC Report Generation")

    print(f"\n{'='*60}")
    print(f"  ✓ Pipeline complete for: {args.dataset}")
    print(f"  Output: 01.Results/ folder")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
