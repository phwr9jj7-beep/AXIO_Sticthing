"""
lib_shared.py  (backward-compatibility re-export shim)
-------------------------------------------------------
All canvas/tile utilities have been migrated to axio_stitching.canvas
and axio_stitching.parsers. This file re-exports them so that the numbered
pipeline scripts (01_inspect_data.py, etc.), lib_stitch_phase.py, and
lib_stitch_sift.py continue to work without modification.

DO NOT add new code here. Make changes in axio_stitching/ modules.
"""

import sys
from pathlib import Path

# Ensure axio_stitching package is importable from scripts/ context
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from axio_stitching.canvas import (          # noqa: F401, E402
    detect_tile_axes,
    read_tile_frame,
    read_tile_channel,
    resolve_tile_filename,
    make_feather_weight,
    stitch_canvas,
    save_tiff,
    save_preview_thumbnail,
)
from axio_stitching.parsers import parse_xml  # noqa: F401, E402

__all__ = [
    "detect_tile_axes",
    "read_tile_frame",
    "read_tile_channel",
    "resolve_tile_filename",
    "make_feather_weight",
    "stitch_canvas",
    "save_tiff",
    "save_preview_thumbnail",
    "parse_xml",
]
