"""
models.py
---------
Pydantic v2 data models for the AXIO Stitching pipeline.
Provides type-safe configuration and structured results.

All parameter names and defaults exactly mirror the gui_runner.py argparse
interface as documented in SPEC.md §2.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field, model_validator, ConfigDict


# ---------------------------------------------------------------------------
# Enumerations — mirror the `choices` in gui_runner.py argparse
# ---------------------------------------------------------------------------

class CorrectionMethod(str, Enum):
    """Shading correction method."""
    BASICPY = "basicpy"
    MEDIAN = "median"
    SPATIAL = "spatial"
    NONE = "none"


class StitchAlgorithm(str, Enum):
    """Tile registration / stitching algorithm."""
    PHASE = "phase"
    SIFT = "sift"
    COORDINATE = "coordinate"


class AlignmentMode(str, Enum):
    """Channel fusion method for multi-channel reference frame construction."""
    REFERENCE = "reference"
    AVERAGE = "average"
    MAX_PROJECTION = "max_projection"


class ZMode(str, Enum):
    """Z-stack stitching mode."""
    NONE = "none"
    MIP_ALIGN_3D = "mip_align_3d"
    REF_SLICE_3D = "ref_slice_3d"
    MIP_OUTPUT_ONLY = "mip_output_only"


# ---------------------------------------------------------------------------
# Tile & Scene metadata
# ---------------------------------------------------------------------------

class TileInfo(BaseModel):
    """Metadata for a single microscopy tile."""
    filename: str
    x: float
    y: float
    w: int
    h: int

    model_config = ConfigDict(frozen=True)


class SceneInfo(BaseModel):
    """Metadata for a single scene (collection of tiles)."""
    scene_id: int
    tiles: list[TileInfo] = Field(default_factory=list)
    cols: int | None = None
    rows: int | None = None

    @property
    def total_tiles(self) -> int:
        return len(self.tiles)

    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Stitching configuration — mirrors gui_runner.py argparse exactly
# ---------------------------------------------------------------------------

class StitchConfig(BaseModel):
    """
    Complete stitching pipeline configuration.

    Parameter names and defaults exactly match those documented in SPEC.md §2
    (gui_runner.py Arguments table). This model is the single source of truth
    for pipeline configuration and is shared by the CLI, MCP server, and GUI.
    """

    # Required
    xml_path: Path = Field(..., description="Absolute path to Zeiss _info.xml or _meta.xml")
    out_dir: Path = Field(..., description="Directory where output files are saved")

    # Algorithm selection
    correction: CorrectionMethod = Field(
        CorrectionMethod.BASICPY,
        description="Illumination / shading correction method"
    )
    algorithm: StitchAlgorithm = Field(
        StitchAlgorithm.PHASE,
        description="Tile registration algorithm"
    )

    # Scene / channel selection
    scene: int | None = Field(None, description="Restrict to single scene (0-indexed). None = all scenes.")
    ref_channel: int = Field(0, ge=0, description="Reference channel index for multi-page TIFF stacks")

    # Split-channel tags
    ref_tag: str = Field("", description="Reference channel filename tag for split-channel TIFFs (e.g. '_c1_')")
    target_tags: list[str] = Field(
        default_factory=list,
        description="Target channel filename tags for split-channel TIFFs"
    )

    # Phase 4 options — Consensus-Channel Alignment & Z-Stack support
    alignment_mode: AlignmentMode = Field(
        AlignmentMode.REFERENCE,
        description="Channel fusion method for alignment reference frame"
    )
    z_mode: ZMode = Field(
        ZMode.NONE,
        description="Z-stack stitching mode"
    )
    ref_z_slice: int = Field(0, ge=0, description="Reference Z-slice index (for ref_slice_3d mode)")

    @model_validator(mode="after")
    def validate_paths(self) -> "StitchConfig":
        if not self.xml_path.exists():
            raise ValueError(f"XML file does not exist: {self.xml_path}")
        return self

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Progress & result types
# ---------------------------------------------------------------------------

class PipelineStage(str, Enum):
    """Named stages within the stitching pipeline."""
    INIT = "init"
    PARSING = "parsing"
    CORRECTION = "correction"
    ALIGNMENT = "alignment"
    CANVAS = "canvas"
    OUTPUT = "output"
    DONE = "done"
    FAILED = "failed"


class ProgressEvent(BaseModel):
    """Real-time progress update emitted during pipeline execution."""
    percent: int = Field(0, ge=0, le=100)
    status_message: str = ""
    stage: PipelineStage = PipelineStage.INIT

    def to_stdout(self) -> str:
        """Format as the [STATUS]/[PROGRESS] lines that gui_worker.py parses."""
        return f"[STATUS] {self.status_message}\n[PROGRESS] {self.percent}"


# Type alias for the progress callback signature
ProgressCallback = Callable[[ProgressEvent], None]


class StitchResult(BaseModel):
    """Structured result returned by StitchingEngine.run()."""
    success: bool
    output_paths: list[Path] = Field(default_factory=list)
    preview_paths: list[Path] = Field(default_factory=list)
    duration_seconds: float = 0.0
    scenes_processed: int = 0
    tiles_processed: int = 0
    error_message: str | None = None

    def to_dict(self) -> dict:
        """JSON-serializable representation (paths as strings)."""
        d = self.model_dump()
        d["output_paths"] = [str(p) for p in self.output_paths]
        d["preview_paths"] = [str(p) for p in self.preview_paths]
        return d


class InspectResult(BaseModel):
    """Result returned by StitchingEngine.inspect_metadata()."""
    xml_path: str
    xml_type: str  # "info" or "meta"
    scenes: list[SceneInfo] = Field(default_factory=list)
    total_scenes: int = 0
    total_tiles: int = 0
    pixel_scale_um: float | None = None  # µm per pixel from _meta.xml

    @model_validator(mode="after")
    def compute_totals(self) -> "InspectResult":
        self.total_scenes = len(self.scenes)
        self.total_tiles = sum(s.total_tiles for s in self.scenes)
        return self

    def to_dict(self) -> dict:
        return self.model_dump()


class ValidationResult(BaseModel):
    """Result returned by StitchingEngine.validate_config()."""
    valid: bool
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict:
        return self.model_dump()
