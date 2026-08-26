"""
tile_sources.py — vendor-neutral tile-position resolution.

The pipeline's engine only needs one thing from a dataset to stitch it: **where each tile
sits**, as a per-scene list of ``{filename, x, y, w, h}`` in pixels. Historically that came
only from a Zeiss ``_info.xml`` / ``_meta.xml`` ([parsers.py][]). This module generalises the
entry point so an AI agent (or the CLI/GUI) can stitch tiles from any of the sources that
dominate the field, auto-detecting which one it was handed:

======================  ===================================================================
Source                  How positions are obtained
======================  ===================================================================
``zeiss``               Delegates to :func:`axio_stitching.parsers.parse_zeiss_xml`.
``fiji``                Fiji/ImageJ **TileConfiguration.txt** (the de-facto interchange
                        format): ``filename; ; (x, y[, z])`` in pixels. The
                        ``*.registered.txt`` variant Fiji writes after optimisation is read
                        too, so a refined layout round-trips.
``ome``                 **OME-TIFF** stage metadata: each tile's ``Plane PositionX/PositionY``
                        converted from stage units to pixels via ``PhysicalSizeX``. Fully
                        self-describing, vendor-independent.
``explicit``            An explicit position list the caller supplies inline or as a JSON
                        file — the universal escape hatch (``{"tiles":[{"filename","x","y"}]}``
                        or a bare list). Positions may be given in pixels or micrometres.
``grid``                A bare folder of TIFFs whose **filenames encode a grid** — ``x00_y01``,
                        ``r0c1``, ``row0_col1``, ``Position012`` (+ an explicit ``--grid-cols``)
                        — laid out from the tile size and an overlap fraction, the model MIST /
                        m2stitch / ASHLAR assume before refinement.
======================  ===================================================================

Detection is conservative and reports its reasoning: :class:`ResolvedSource` carries the
chosen ``source_type``, a ``confidence``, the pixel scale when known, and human-readable
``warnings`` and ``notes`` so a caller never has to guess why a layout came out the way it did.
The stage coordinates are a *starting* layout — exactly like Fiji's "compute overlap" or
m2stitch's grid guess — which the ``phase`` / ``sift`` registration then refines. Only
``coordinate`` mode trusts them verbatim.

[parsers.py]: parse_zeiss_xml
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

#: Fallback tile pixel size when a source names no size and no tile file is readable.
_DEFAULT_TILE_PX = 1020

#: Stage-unit -> micrometre factors for the units OME-XML and stage files use.
_UNIT_TO_UM = {
    "m": 1e6, "meter": 1e6, "metre": 1e6,
    "cm": 1e4,
    "mm": 1e3, "millimeter": 1e3, "millimetre": 1e3,
    "um": 1.0, "µm": 1.0, "micron": 1.0, "micrometer": 1.0, "micrometre": 1.0,
    "nm": 1e-3, "nanometer": 1e-3, "nanometre": 1e-3,
    "px": None, "pixel": None, "pixels": None,  # already pixels
}


@dataclass
class ResolvedSource:
    """Everything the engine and the estimator need, plus why it was resolved this way."""
    scenes: dict[int, list[dict]]
    raw_dir: Path
    source_type: str  # 'zeiss' | 'fiji' | 'ome' | 'explicit' | 'grid'
    confidence: str = "high"  # 'high' | 'medium' | 'low'
    pixel_scale_um: float | None = None
    tile_width: int | None = None
    tile_height: int | None = None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_tiles(self) -> int:
        return sum(len(t) for t in self.scenes.values())

    def to_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "confidence": self.confidence,
            "raw_dir": str(self.raw_dir),
            "pixel_scale_um": self.pixel_scale_um,
            "tile_width": self.tile_width,
            "tile_height": self.tile_height,
            "total_scenes": len(self.scenes),
            "total_tiles": self.total_tiles,
            "notes": self.notes,
            "warnings": self.warnings,
        }


class TileSourceError(ValueError):
    """Raised when no supported source can be resolved from the given path."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve_tiles(
    source: str | Path,
    *,
    positions: list[dict] | None = None,
    overlap: float = 0.1,
    grid_cols: int | None = None,
    serpentine: bool = True,
    tile_size: tuple[int, int] | None = None,
    pixel_size_um: float | None = None,
) -> ResolvedSource:
    """
    Resolve tile positions from any supported source, auto-detecting the format.

    Args:
        source: A file (Zeiss XML, TileConfiguration.txt, positions .json, or one OME-TIFF)
            or a directory of tiles.
        positions: An explicit position list, taking precedence over ``source`` detection.
            Each item is ``{"filename", "x", "y"[, "scene"]}``.
        overlap: Tile overlap fraction (0-1) used only by the filename-grid layout.
        grid_cols: Number of columns, for filename patterns that carry a linear position
            index (e.g. Micro-Manager ``Position012``) rather than explicit row/col.
        serpentine: Whether a linear position index snakes (boustrophedon) rather than
            raster-scanning. Only used with ``grid_cols``.
        tile_size: ``(width, height)`` in pixels, overriding what is read from a sample tile.
        pixel_size_um: Micrometres per pixel, overriding what a source declares (needed to
            convert stage-unit positions when a source omits its scale).

    Returns:
        A :class:`ResolvedSource`.

    Raises:
        TileSourceError: when nothing supported is found (the message lists what was tried).
    """
    if positions:
        return _from_explicit(positions, Path(source), tile_size, pixel_size_um)

    path = Path(source).expanduser()
    if not path.exists():
        raise TileSourceError(f"source does not exist: {path}")

    if path.is_dir():
        return _resolve_directory(
            path, overlap=overlap, grid_cols=grid_cols, serpentine=serpentine,
            tile_size=tile_size, pixel_size_um=pixel_size_um,
        )
    return _resolve_file(
        path, overlap=overlap, grid_cols=grid_cols, serpentine=serpentine,
        tile_size=tile_size, pixel_size_um=pixel_size_um,
    )


def detect_source_type(source: str | Path) -> str:
    """
    Cheaply classify a source without fully parsing it — for the ``axio_inspect_dataset``
    preamble and for error messages. Returns one of the source-type strings, or 'unknown'.
    """
    path = Path(source).expanduser()
    if not path.exists():
        return "unknown"
    if path.is_file():
        return _classify_file(path)
    # Directory: name the strongest signal present.
    if _find_fiji_config(path):
        return "fiji"
    if _find_zeiss_xml(path):
        return "zeiss"
    if _first_ome_with_position(_list_tiles(path)):
        return "ome"
    if _list_tiles(path):
        return "grid"
    return "unknown"


# ---------------------------------------------------------------------------
# File dispatch
# ---------------------------------------------------------------------------

def _classify_file(path: Path) -> str:
    name = path.name.lower()
    if name.endswith("_info.xml") or name.endswith("_meta.xml"):
        return "zeiss"
    if name.startswith("tileconfiguration") and name.endswith(".txt"):
        return "fiji"
    if name.endswith(".json"):
        return "explicit"
    if name.endswith((".ome.tif", ".ome.tiff")):
        return "ome"
    if name.endswith(".xml"):
        return "zeiss"  # best guess for a bare .xml
    if name.endswith(".txt"):
        return "fiji"
    if name.endswith((".tif", ".tiff")):
        return "ome"  # a single tiff: only useful if it carries OME positions
    return "unknown"


def _resolve_file(path: Path, **kw) -> ResolvedSource:
    kind = _classify_file(path)
    if kind == "zeiss":
        return _from_zeiss(path)
    if kind == "fiji":
        return _from_fiji(path, kw.get("tile_size"))
    if kind == "explicit":
        return _from_json(path, kw.get("tile_size"), kw.get("pixel_size_um"))
    if kind == "ome":
        # A single OME-TIFF is only a dataset if it holds several positioned planes; otherwise
        # treat its directory as the source.
        return _resolve_directory(path.parent, **kw)
    raise TileSourceError(
        f"unrecognised source file: {path.name}. Supported: Zeiss _info.xml/_meta.xml, "
        "a Fiji TileConfiguration.txt, a positions .json, an OME-TIFF, or a directory of tiles."
    )


def _resolve_directory(path: Path, **kw) -> ResolvedSource:
    """A directory: prefer an explicit layout file, then OME positions, then a filename grid."""
    fiji = _find_fiji_config(path)
    if fiji:
        result = _from_fiji(fiji, kw.get("tile_size"))
        result.notes.insert(0, f"used the tile layout in {fiji.name}")
        return result

    zeiss = _find_zeiss_xml(path)
    if zeiss:
        result = _from_zeiss(zeiss)
        result.notes.insert(0, f"used the Zeiss metadata {zeiss.name} found in the directory")
        return result

    tiles = _list_tiles(path)
    if not tiles:
        raise TileSourceError(
            f"no tiles or layout found in {path}. Provide a Fiji TileConfiguration.txt, a "
            "Zeiss _info.xml, OME-TIFFs with stage positions, or an explicit positions list."
        )

    if _first_ome_with_position(tiles):
        return _from_ome(tiles, path, kw.get("pixel_size_um"), kw.get("tile_size"))

    return _from_filename_grid(
        tiles, path,
        overlap=kw.get("overlap", 0.1),
        grid_cols=kw.get("grid_cols"),
        serpentine=kw.get("serpentine", True),
        tile_size=kw.get("tile_size"),
    )


# ---------------------------------------------------------------------------
# Directory scanning helpers
# ---------------------------------------------------------------------------

_TIFF_SUFFIXES = (".tif", ".tiff")


def _list_tiles(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in _TIFF_SUFFIXES
    )


def _find_fiji_config(directory: Path) -> Path | None:
    # Prefer a registered (refined) config over the raw one.
    candidates = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.name.lower().startswith("tileconfiguration") and p.suffix.lower() == ".txt"
    )
    registered = [p for p in candidates if "registered" in p.name.lower()]
    return (registered or candidates or [None])[0]


def _find_zeiss_xml(directory: Path) -> Path | None:
    for suffix in ("_info.xml", "_meta.xml"):
        matches = sorted(p for p in directory.iterdir() if p.is_file() and p.name.lower().endswith(suffix))
        if matches:
            return matches[0]
    return None


def _read_tile_size(sample: Path, override: tuple[int, int] | None) -> tuple[int, int, str | None]:
    """``(width, height, note)`` — from override, else a real tile, else the default."""
    if override:
        return int(override[0]), int(override[1]), None
    try:
        from .canvas import detect_tile_axes

        info = detect_tile_axes(sample)
        return int(info["W"]), int(info["H"]), None
    except Exception as exc:  # noqa: BLE001 - a broken sample must not sink resolution
        return (
            _DEFAULT_TILE_PX,
            _DEFAULT_TILE_PX,
            f"could not read tile size from {sample.name} ({exc}); assumed {_DEFAULT_TILE_PX}px",
        )


# ---------------------------------------------------------------------------
# zeiss
# ---------------------------------------------------------------------------

def _from_zeiss(path: Path) -> ResolvedSource:
    from .parsers import parse_zeiss_xml

    scenes, xml_type, pixel_scale_um = parse_zeiss_xml(path)
    tile_w = tile_h = None
    for tiles in scenes.values():
        if tiles:
            tile_w, tile_h = int(tiles[0]["w"]), int(tiles[0]["h"])
            break
    return ResolvedSource(
        scenes=scenes,
        raw_dir=path.parent,
        source_type="zeiss",
        confidence="high",
        pixel_scale_um=pixel_scale_um,
        tile_width=tile_w,
        tile_height=tile_h,
        notes=[f"parsed Zeiss {xml_type}.xml"],
    )


# ---------------------------------------------------------------------------
# fiji TileConfiguration.txt
# ---------------------------------------------------------------------------

_FIJI_LINE = re.compile(
    r"^\s*(?P<file>.+?)\s*;\s*(?P<region>[^;]*)\s*;\s*\(\s*(?P<coords>[-0-9.eE,\s]+)\)\s*$"
)


def _from_fiji(path: Path, tile_size: tuple[int, int] | None) -> ResolvedSource:
    """
    Parse a Fiji/ImageJ TileConfiguration.txt (or its ``.registered.txt`` variant).

    Positions are already in pixels (the format's contract), so no scaling is applied.
    Multi-region configs (Fiji writes one block per grid) are split into scenes by the
    optional ``region`` column when present.
    """
    warnings: list[str] = []
    scenes: dict[int, list[dict]] = {}
    raw_dir = path.parent
    tiles_by_region: dict[str, list[dict]] = {}

    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("dim"):
            continue
        m = _FIJI_LINE.match(stripped)
        if not m:
            continue
        coords = [c.strip() for c in m.group("coords").split(",") if c.strip()]
        try:
            x = float(coords[0])
            y = float(coords[1])
        except (IndexError, ValueError):
            warnings.append(f"skipped malformed line: {stripped[:60]}")
            continue
        region = m.group("region").strip() or "0"
        tiles_by_region.setdefault(region, []).append(
            {"filename": m.group("file").strip(), "x": x, "y": y}
        )

    if not tiles_by_region:
        raise TileSourceError(
            f"{path.name} contained no parseable 'file; ; (x, y)' lines. Is it a Fiji "
            "TileConfiguration file?"
        )

    # Establish tile size from a real tile.
    sample = None
    for region_tiles in tiles_by_region.values():
        candidate = raw_dir / region_tiles[0]["filename"]
        if candidate.exists():
            sample = candidate
            break
    tile_w, tile_h, size_note = _read_tile_size(sample or (raw_dir / next(iter(tiles_by_region.values()))[0]["filename"]), tile_size)
    if size_note:
        warnings.append(size_note)

    for scene_id, (_region, region_tiles) in enumerate(sorted(tiles_by_region.items())):
        # Normalise so the minimum corner is the origin (Fiji configs can be negative).
        min_x = min(t["x"] for t in region_tiles)
        min_y = min(t["y"] for t in region_tiles)
        scenes[scene_id] = [
            {"filename": t["filename"], "x": t["x"] - min_x, "y": t["y"] - min_y, "w": tile_w, "h": tile_h}
            for t in region_tiles
        ]

    return ResolvedSource(
        scenes=scenes,
        raw_dir=raw_dir,
        source_type="fiji",
        confidence="high",
        tile_width=tile_w,
        tile_height=tile_h,
        notes=[f"parsed Fiji TileConfiguration ({sum(len(t) for t in scenes.values())} tiles"
               f"{', registered/refined' if 'registered' in path.name.lower() else ''})"],
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# ome-tiff stage positions
# ---------------------------------------------------------------------------

_OME_POS = re.compile(
    r'<Plane\b[^>]*\bPositionX="(?P<px>[-0-9.eE]+)"[^>]*\bPositionY="(?P<py>[-0-9.eE]+)"',
    re.IGNORECASE,
)
_OME_POS_REV = re.compile(
    r'<Plane\b[^>]*\bPositionY="(?P<py>[-0-9.eE]+)"[^>]*\bPositionX="(?P<px>[-0-9.eE]+)"',
    re.IGNORECASE,
)
_OME_POSX_UNIT = re.compile(r'PositionXUnit="([^"]+)"', re.IGNORECASE)
_OME_PHYS_X = re.compile(r'PhysicalSizeX="([0-9.eE]+)"', re.IGNORECASE)
_OME_PHYS_X_UNIT = re.compile(r'PhysicalSizeXUnit="([^"]+)"', re.IGNORECASE)


def _ome_position(tif_path: Path) -> tuple[float, float, float | None, str | None] | None:
    """``(pos_x, pos_y, physical_size_um, pos_unit)`` from a tile's OME-XML, or None."""
    import tifffile

    try:
        with tifffile.TiffFile(str(tif_path)) as tf:
            if not tf.is_ome or not tf.ome_metadata:
                return None
            ome = tf.ome_metadata
    except Exception:
        return None

    match = _OME_POS.search(ome) or _OME_POS_REV.search(ome)
    if not match:
        return None
    pos_x = float(match.group("px"))
    pos_y = float(match.group("py"))

    phys_um = None
    phys = _OME_PHYS_X.search(ome)
    if phys:
        unit = (_OME_PHYS_X_UNIT.search(ome) or [None, "um"])[1]
        factor = _UNIT_TO_UM.get((unit or "um").lower(), 1.0)
        phys_um = float(phys.group(1)) * (factor if factor is not None else 1.0)

    pos_unit = (_OME_POSX_UNIT.search(ome) or [None, None])[1]
    return pos_x, pos_y, phys_um, pos_unit


def _first_ome_with_position(tiles: list[Path]) -> bool:
    for tile in tiles[:12]:
        if _ome_position(tile) is not None:
            return True
    return False


def _from_ome(
    tiles: list[Path], directory: Path, pixel_size_um: float | None, tile_size: tuple[int, int] | None
) -> ResolvedSource:
    """Lay tiles out from their embedded OME stage positions, converted to pixels."""
    warnings: list[str] = []
    records: list[tuple[Path, float, float, str | None]] = []
    phys_um: float | None = pixel_size_um

    for tile in tiles:
        pos = _ome_position(tile)
        if pos is None:
            continue
        pos_x, pos_y, sample_phys, pos_unit = pos
        if phys_um is None and sample_phys:
            phys_um = sample_phys
        records.append((tile, pos_x, pos_y, pos_unit))

    if not records:
        raise TileSourceError("no OME-TIFF in the directory carried a Plane PositionX/PositionY")

    tile_w, tile_h, size_note = _read_tile_size(records[0][0], tile_size)
    if size_note:
        warnings.append(size_note)

    if phys_um is None:
        phys_um = 1.0
        warnings.append(
            "no PhysicalSizeX in the OME metadata and no pixel_size_um given; assuming stage "
            "positions are already in pixels. Pass pixel_size_um if the mosaic looks wrongly scaled."
        )

    # Convert stage positions to pixels. If a position unit is present, honour it; otherwise
    # assume the positions share PhysicalSizeX's unit (micrometres after conversion).
    pixel_records: list[tuple[str, float, float]] = []
    for tile, pos_x, pos_y, pos_unit in records:
        unit_factor = _UNIT_TO_UM.get((pos_unit or "um").lower(), 1.0)
        if unit_factor is None:  # positions already in pixels
            px, py = pos_x, pos_y
        else:
            px = pos_x * unit_factor / phys_um
            py = pos_y * unit_factor / phys_um
        pixel_records.append((tile.name, px, py))

    min_x = min(r[1] for r in pixel_records)
    min_y = min(r[2] for r in pixel_records)
    scene_tiles = [
        {"filename": name, "x": px - min_x, "y": py - min_y, "w": tile_w, "h": tile_h}
        for name, px, py in pixel_records
    ]

    return ResolvedSource(
        scenes={0: scene_tiles},
        raw_dir=directory,
        source_type="ome",
        confidence="high",
        pixel_scale_um=phys_um,
        tile_width=tile_w,
        tile_height=tile_h,
        notes=[f"read stage positions from {len(scene_tiles)} OME-TIFF tiles"
               f" at {phys_um:.4g} µm/px"],
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# explicit positions (inline list or JSON file)
# ---------------------------------------------------------------------------

def _from_json(path: Path, tile_size: tuple[int, int] | None, pixel_size_um: float | None) -> ResolvedSource:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TileSourceError(f"could not read positions JSON {path.name}: {exc}") from exc

    if isinstance(data, dict):
        tiles = data.get("tiles") or data.get("positions")
        pixel_size_um = pixel_size_um or data.get("pixel_size_um")
        units = data.get("units")
    elif isinstance(data, list):
        tiles, units = data, None
    else:
        raise TileSourceError(f"{path.name} must be a list or an object with a 'tiles' array")

    if not tiles:
        raise TileSourceError(f"{path.name} contained no tile positions")

    result = _from_explicit(tiles, path, tile_size, pixel_size_um, units=units)
    result.notes.insert(0, f"read {result.total_tiles} positions from {path.name}")
    return result


def _from_explicit(
    positions: list[dict],
    source: Path,
    tile_size: tuple[int, int] | None,
    pixel_size_um: float | None,
    units: str | None = None,
) -> ResolvedSource:
    """Build scenes from an explicit ``[{filename, x, y[, scene]}]`` list."""
    warnings: list[str] = []
    raw_dir = source if source.is_dir() else source.parent

    unit_factor = 1.0
    if units and units.lower() in _UNIT_TO_UM:
        stage_um = _UNIT_TO_UM[units.lower()]
        if stage_um is not None:  # positions in stage units -> need pixel size
            if not pixel_size_um:
                warnings.append(
                    f"positions are in {units} but no pixel_size_um was given; treated as pixels"
                )
            else:
                unit_factor = stage_um / pixel_size_um

    by_scene: dict[int, list[dict]] = {}
    for item in positions:
        # Validate the whole record BEFORE inserting, so a bad entry cannot leave an empty
        # scene behind (a setdefault that runs before a KeyError would).
        try:
            scene = int(item.get("scene", 0))
            record = {
                "filename": str(item["filename"]),
                "x": float(item["x"]) * unit_factor,
                "y": float(item["y"]) * unit_factor,
            }
        except (KeyError, TypeError, ValueError) as exc:
            warnings.append(f"skipped an invalid position entry ({exc}): {str(item)[:60]}")
            continue
        by_scene.setdefault(scene, []).append(record)

    if not by_scene:
        raise TileSourceError("no valid {filename, x, y} entries in the explicit positions")

    sample = None
    for tiles in by_scene.values():
        candidate = raw_dir / tiles[0]["filename"]
        if candidate.exists():
            sample = candidate
            break
    tile_w, tile_h, size_note = _read_tile_size(
        sample or raw_dir / next(iter(by_scene.values()))[0]["filename"], tile_size
    )
    if size_note:
        warnings.append(size_note)

    scenes: dict[int, list[dict]] = {}
    for scene_id, tiles in sorted(by_scene.items()):
        min_x = min(t["x"] for t in tiles)
        min_y = min(t["y"] for t in tiles)
        scenes[scene_id] = [
            {"filename": t["filename"], "x": t["x"] - min_x, "y": t["y"] - min_y, "w": tile_w, "h": tile_h}
            for t in tiles
        ]

    return ResolvedSource(
        scenes=scenes, raw_dir=raw_dir, source_type="explicit", confidence="high",
        pixel_scale_um=pixel_size_um, tile_width=tile_w, tile_height=tile_h, warnings=warnings,
    )


# ---------------------------------------------------------------------------
# filename-encoded grid
# ---------------------------------------------------------------------------

#: Ordered patterns for extracting (col, row) or a linear index from a filename. The first
#: that matches every candidate tile wins, so an unambiguous scheme is preferred.
_GRID_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("xy",   re.compile(r"[_\-]x(\d+)[_\-]?y(\d+)", re.IGNORECASE), "col_row"),
    ("yx",   re.compile(r"[_\-]y(\d+)[_\-]?x(\d+)", re.IGNORECASE), "row_col"),
    ("rc",   re.compile(r"[_\-]r(?:ow)?(\d+)[_\-]?c(?:ol)?(\d+)", re.IGNORECASE), "row_col"),
    ("index", re.compile(r"(?:position|pos|tile|p|m|f)[_\-]?(\d+)", re.IGNORECASE), "index"),
)


def _from_filename_grid(
    tiles: list[Path],
    directory: Path,
    *,
    overlap: float,
    grid_cols: int | None,
    serpentine: bool,
    tile_size: tuple[int, int] | None,
) -> ResolvedSource:
    """
    Lay a bare folder of TIFFs out on a grid inferred from their filenames.

    This is a *starting* layout — the same assumption MIST / m2stitch / ASHLAR make before
    refining with image content — so the caller should stitch it with ``phase`` or ``sift``,
    not ``coordinate``. The overlap fraction sets the tile pitch.
    """
    warnings: list[str] = []
    notes: list[str] = []
    names = [t.name for t in tiles]

    chosen = None
    for label, pattern, meaning in _GRID_PATTERNS:
        matches = [pattern.search(n) for n in names]
        if all(matches):
            chosen = (label, meaning, matches)
            break

    if chosen is None:
        raise TileSourceError(
            "the filenames do not encode a recognisable grid (tried x#/y#, r#/c#, and a "
            "position index). Provide a Fiji TileConfiguration.txt, OME-TIFFs with stage "
            "positions, or an explicit positions list. Filenames seen: "
            + ", ".join(names[:4]) + (" ..." if len(names) > 4 else "")
        )

    label, meaning, matches = chosen
    tile_w, tile_h, size_note = _read_tile_size(tiles[0], tile_size)
    if size_note:
        warnings.append(size_note)

    step_x = int(round(tile_w * (1.0 - overlap)))
    step_y = int(round(tile_h * (1.0 - overlap)))

    cols_rows: list[tuple[int, int]] = []
    if meaning == "index":
        indices = [int(m.group(1)) for m in matches]
        base = min(indices)
        if not grid_cols:
            # A linear index with no column count cannot be laid out as a grid.
            raise TileSourceError(
                f"filenames carry a linear position index ({label}) but no column count was "
                "given; pass grid_cols=N (and the images' overlap) so the grid can be built, "
                "or supply a TileConfiguration/positions list."
            )
        for idx in indices:
            n = idx - base
            row = n // grid_cols
            col = n % grid_cols
            if serpentine and row % 2 == 1:
                col = grid_cols - 1 - col
            cols_rows.append((col, row))
        notes.append(
            f"laid out a {grid_cols}-column {'serpentine' if serpentine else 'raster'} grid "
            f"from the position index in filenames"
        )
    else:
        for m in matches:
            a, b = int(m.group(1)), int(m.group(2))
            col, row = (a, b) if meaning == "col_row" else (b, a)
            cols_rows.append((col, row))
        notes.append(f"laid out a grid from the {label} indices in filenames")

    min_col = min(c for c, _ in cols_rows)
    min_row = min(r for _, r in cols_rows)
    scene_tiles = [
        {
            "filename": name,
            "x": float((col - min_col) * step_x),
            "y": float((row - min_row) * step_y),
            "w": tile_w,
            "h": tile_h,
        }
        for name, (col, row) in zip(names, cols_rows)
    ]

    notes.append(f"assumed {overlap:.0%} overlap (pitch {step_x}x{step_y}px); "
                 "stitch with phase/sift to refine")
    return ResolvedSource(
        scenes={0: scene_tiles},
        raw_dir=directory,
        source_type="grid",
        confidence="low",
        tile_width=tile_w,
        tile_height=tile_h,
        notes=notes,
        warnings=warnings,
    )
