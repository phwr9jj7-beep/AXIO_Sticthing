"""
gui_runner.py  (backward-compatible CLI shim)
--------------------------------------------
This file is a thin wrapper that preserves the exact command-line interface
documented in SPEC.md §2. All stitching logic has been extracted to the
`axio_stitching` package.

Original: 940 lines — now ~60 lines.
No argparse argument names, defaults, or behaviors have changed.
"""

import sys
import argparse
from pathlib import Path

# Ensure axio_stitching package is importable when invoked as subprocess
# (gui_worker.py spawns this script directly via Popen from scripts/)
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Handle PyInstaller frozen exe context
if getattr(sys, "frozen", False):
    import os
    _bundle_dir = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    if str(_bundle_dir) not in sys.path:
        sys.path.insert(0, str(_bundle_dir))

from axio_stitching import StitchingEngine
from axio_stitching.models import StitchConfig, ProgressEvent


def _make_progress_callback():
    """Returns a callback that prints [STATUS]/[PROGRESS] lines for gui_worker.py to parse."""
    def callback(event: ProgressEvent) -> None:
        print(f"[STATUS] {event.status_message}", flush=True)
        print(f"[PROGRESS] {event.percent}", flush=True)
    return callback


def main() -> None:
    parser = argparse.ArgumentParser(description="AXIO Stitching GUI Custom Runner")
    parser.add_argument("--xml", required=True, help="Path to Zeiss XML file (_info.xml or _meta.xml)")
    parser.add_argument("--out-dir", required=True, help="Directory to save output files")
    parser.add_argument("--correction", choices=["basicpy", "median", "spatial", "none"],
                        default="basicpy", help="Illumination correction method")
    parser.add_argument("--algorithm", choices=["phase", "coordinate", "sift"],
                        default="phase", help="Stitching algorithm")
    parser.add_argument("--scene", type=int, default=None,
                        help="Restrict to single scene (0-indexed)")
    parser.add_argument("--ref-channel", type=int, default=0,
                        help="Reference channel index for multi-page TIFF stacks")
    parser.add_argument("--ref-tag", type=str, default="",
                        help="Reference channel filename tag for split channel TIFFs")
    parser.add_argument("--target-tags", type=str, default="",
                        help="Target channel filename tags, comma separated")
    parser.add_argument("--alignment-mode", choices=["reference", "average", "max_projection"],
                        default="reference", help="Channel fusion method for alignment")
    parser.add_argument("--z-mode", choices=["none", "mip_align_3d", "ref_slice_3d", "mip_output_only"],
                        default="none", help="Z-stack handling mode")
    parser.add_argument("--ref-z-slice", type=int, default=0,
                        help="Reference Z-slice index (for ref_slice_3d mode)")
    args = parser.parse_args()

    target_tags_list = [t.strip() for t in args.target_tags.split(",") if t.strip()] if args.target_tags else []

    config = StitchConfig(
        xml_path=Path(args.xml).resolve(),
        out_dir=Path(args.out_dir).resolve(),
        correction=args.correction,
        algorithm=args.algorithm,
        scene=args.scene,
        ref_channel=args.ref_channel,
        ref_tag=args.ref_tag,
        target_tags=target_tags_list,
        alignment_mode=args.alignment_mode,
        z_mode=args.z_mode,
        ref_z_slice=args.ref_z_slice,
    )

    engine = StitchingEngine(config, progress_callback=_make_progress_callback())
    result = engine.run()

    if not result.success:
        print(f"[ERROR] {result.error_message}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
