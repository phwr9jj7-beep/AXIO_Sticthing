"""
axio_stitching
--------------
High-throughput spatial microscopy stitching pipeline for Zeiss Axio Tile Scans.

Exports:
    StitchingEngine   – Programmatic API for the full stitching pipeline.
    StitchConfig      – Pydantic model for all stitching parameters.
    StitchResult      – Pydantic model for pipeline results.
    __version__       – Package version string.
"""

__version__ = "1.1.0"
__author__ = "Ziyi Wong"
__license__ = "MIT"

from .engine import StitchingEngine
from .models import (
    StitchConfig,
    StitchResult,
    ProgressEvent,
    SceneInfo,
    TileInfo,
    CorrectionMethod,
    StitchAlgorithm,
    AlignmentMode,
    ZMode,
)

__all__ = [
    "StitchingEngine",
    "StitchConfig",
    "StitchResult",
    "ProgressEvent",
    "SceneInfo",
    "TileInfo",
    "CorrectionMethod",
    "StitchAlgorithm",
    "AlignmentMode",
    "ZMode",
    "__version__",
]
