"""
generate_icon.py — the AXIO Stitching Studio icon and logo, generated, not hand-drawn.

The mark is the product's own story: four translucent tiles overlapping into a brighter
feathered cross — a tile scan being stitched. Overlaps get brighter because each tile is
drawn semi-transparent over a dark ground, exactly like feathered blending does.

Outputs (all committed artefacts are reproducible from this one script):

    assets/icon.svg        vector master of the mark
    assets/icon.png        256 px raster (README, social embeds)
    assets/logo.svg        horizontal wordmark for documentation headers
    installer/icon.ico     multi-size Windows icon (16..256) for the EXEs and the installer

Run:  python scripts/generate_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "assets"
INSTALLER = REPO / "installer"

# ---------------------------------------------------------------------------
# Palette — the GUI's dark navy + blue/cyan accents.
# ---------------------------------------------------------------------------

BG = (11, 18, 32, 255)          # near-black navy (#0B1220)
#: Per tile (TL, TR, BL, BR): fill RGBA, stroke RGBA, rotation in degrees. The last tile
#: is tilted a few degrees — the tile still being registered into place.
TILES = (
    ((125, 211, 252, 118), (186, 230, 253, 235), 0.0),   # sky    #7DD3FC
    ((99, 102, 241, 118),  (165, 180, 252, 235), 0.0),   # indigo #6366F1
    ((37, 99, 235, 118),   (147, 197, 253, 235), 0.0),   # blue   #2563EB
    ((45, 212, 191, 130),  (153, 246, 228, 245), -5.0),  # teal   #2DD4BF (mid-alignment)
)

#: Master raster size. Everything scales from here.
SIZE = 1024
#: Tile geometry at master scale: side, corner radius, and the overlap between neighbours.
TILE = 430
TILE_RADIUS = 72
OVERLAP = 118
#: Background rounded-rect corner radius.
BG_RADIUS = 200


def _tile_origins(size: int, tile: int, overlap: int) -> list[tuple[int, int]]:
    """Top-left corners of the 2x2 tile grid, centred on the canvas."""
    pitch = tile - overlap
    total = tile + pitch
    x0 = (size - total) // 2
    y0 = (size - total) // 2
    return [(x0, y0), (x0 + pitch, y0), (x0, y0 + pitch), (x0 + pitch, y0 + pitch)]


def draw_master(with_background: bool = True) -> Image.Image:
    """The mark at master resolution, drawn tile-over-tile so overlaps brighten."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    if with_background:
        bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        ImageDraw.Draw(bg).rounded_rectangle(
            (0, 0, SIZE - 1, SIZE - 1), radius=BG_RADIUS, fill=BG
        )
        img = Image.alpha_composite(img, bg)

    for (ox, oy), (fill, stroke, angle) in zip(_tile_origins(SIZE, TILE, OVERLAP), TILES):
        layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            (ox, oy, ox + TILE - 1, oy + TILE - 1), radius=TILE_RADIUS,
            fill=fill, outline=stroke, width=14,
        )
        if angle:
            layer = layer.rotate(
                angle, resample=Image.BICUBIC,
                center=(ox + TILE / 2, oy + TILE / 2),
            )
        img = Image.alpha_composite(img, layer)
    return img


def write_ico(master: Image.Image, path: Path) -> None:
    """Multi-size .ico downscaled from the master with LANCZOS (crisp at 16 px)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    master.resize((256, 256), Image.LANCZOS).save(
        path, format="ICO", sizes=[(s, s) for s in sizes]
    )


# ---------------------------------------------------------------------------
# Vector versions — the same geometry, hand-carried into SVG.
# ---------------------------------------------------------------------------

def _svg_tiles(scale: float = 1.0, dx: float = 0.0, dy: float = 0.0) -> str:
    parts = []
    hexes = (
        ("#7DD3FC", "#BAE6FD", 0.0),
        ("#6366F1", "#A5B4FC", 0.0),
        ("#2563EB", "#93C5FD", 0.0),
        ("#2DD4BF", "#99F6E4", -5.0),
    )
    for (ox, oy), (fill, stroke, angle) in zip(_tile_origins(SIZE, TILE, OVERLAP), hexes):
        x, y = ox * scale + dx, oy * scale + dy
        w = TILE * scale
        transform = (
            f' transform="rotate({angle:.1f} {x + w / 2:.1f} {y + w / 2:.1f})"' if angle else ""
        )
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{w:.1f}" '
            f'rx="{TILE_RADIUS * scale:.1f}" fill="{fill}" fill-opacity="0.46" '
            f'stroke="{stroke}" stroke-opacity="0.92" stroke-width="{14 * scale:.1f}"{transform}/>'
        )
    return "\n  ".join(parts)


def write_icon_svg(path: Path) -> None:
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}">
  <rect width="{SIZE}" height="{SIZE}" rx="{BG_RADIUS}" fill="#0B1220"/>
  {_svg_tiles()}
</svg>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")


def write_logo_svg(path: Path) -> None:
    """Horizontal wordmark: the mark at 96 px beside the product name."""
    mark_scale = 96 / SIZE
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 560 120" role="img"
     aria-label="AXIO Stitching Studio">
  <rect x="8" y="12" width="96" height="96" rx="{BG_RADIUS * mark_scale:.1f}" fill="#0B1220"/>
  {_svg_tiles(scale=mark_scale, dx=8, dy=12)}
  <text x="124" y="66" font-family="'Segoe UI', Inter, Arial, sans-serif" font-size="42"
        font-weight="700" fill="#E2E8F0">AXIO</text>
  <text x="240" y="66" font-family="'Segoe UI', Inter, Arial, sans-serif" font-size="42"
        font-weight="300" fill="#94A3B8">Stitching Studio</text>
  <text x="124" y="94" font-family="'Segoe UI', Inter, Arial, sans-serif" font-size="17"
        fill="#64748B">tile scans in &#8226; mosaics out &#8226; humans, scripts &amp; AI agents</text>
</svg>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")


def _font(size: int, bold: bool = False):
    """A real system font, tried in preference order; SVG font stacks are not reliable in
    every renderer, so the README uses this deterministic PNG wordmark instead."""
    from PIL import ImageFont

    candidates = (
        ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"] if bold
        else ["segoeui.ttf", "arial.ttf"]
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def write_logo_png(master: Image.Image, path: Path, dark_background: bool) -> None:
    """
    Horizontal wordmark PNG (2x for crisp README rendering): mark + product name.

    Two variants because GitHub renders READMEs on both themes: ``dark_background=True``
    uses light text (for dark mode), False uses slate text (for light mode). The README
    switches between them with a ``<picture>`` element.
    """
    if dark_background:
        name_fill, sub_fill, tag_fill = (226, 232, 240, 255), (148, 163, 184, 255), (100, 116, 139, 255)
    else:
        name_fill, sub_fill, tag_fill = (30, 41, 59, 255), (71, 85, 105, 255), (100, 116, 139, 255)

    height = 240
    img = Image.new("RGBA", (1160, height), (0, 0, 0, 0))
    mark = master.resize((192, 192), Image.LANCZOS)
    img.alpha_composite(mark, (16, (height - 192) // 2))
    draw = ImageDraw.Draw(img)
    x = 244
    draw.text((x, 62), "AXIO", font=_font(84, bold=True), fill=name_fill)
    axio_w = draw.textlength("AXIO ", font=_font(84, bold=True))
    draw.text((x + axio_w, 62), "Stitching Studio", font=_font(84), fill=sub_fill)
    draw.text(
        (x + 4, 158),
        "tile scans in  •  mosaics out  •  humans, scripts & AI agents",
        font=_font(34), fill=tag_fill,
    )
    img.save(path)


def main() -> None:
    master = draw_master()
    ASSETS.mkdir(parents=True, exist_ok=True)
    master.resize((256, 256), Image.LANCZOS).save(ASSETS / "icon.png")
    write_ico(master, INSTALLER / "icon.ico")
    write_icon_svg(ASSETS / "icon.svg")
    write_logo_svg(ASSETS / "logo.svg")
    write_logo_png(master, ASSETS / "logo.png", dark_background=False)
    write_logo_png(master, ASSETS / "logo-dark.png", dark_background=True)
    for artefact in (
        ASSETS / "icon.png", ASSETS / "icon.svg", ASSETS / "logo.svg",
        ASSETS / "logo.png", ASSETS / "logo-dark.png", INSTALLER / "icon.ico",
    ):
        print(f"wrote {artefact.relative_to(REPO)}  ({artefact.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
