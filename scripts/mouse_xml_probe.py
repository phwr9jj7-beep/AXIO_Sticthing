"""
mouse_xml_probe.py
------------------
Diagnostic utility to probe and compare different Zeiss AXIO XML formats
(_info.xml vs _meta.xml) to identify schema differences.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict

# Compare old vs new XML format
old_xml = Path('00.RawData/2026_04_17__18_55__0347/2026_04_17__18_55__0347_info.xml')
tree = ET.parse(old_xml)
root = tree.getroot()
images = root.findall('Image')
print('OLD XML: total image entries:', len(images))

scenes = defaultdict(list)
for img in images:
    b = img.find('Bounds')
    if b is not None:
        s = int(b.attrib.get('StartS', 0))
        x = int(b.attrib.get('StartX', 0))
        y = int(b.attrib.get('StartY', 0))
        w = int(b.attrib.get('SizeX', 0))
        h = int(b.attrib.get('SizeY', 0))
        scenes[s].append({'x': x, 'y': y, 'w': w, 'h': h})

for s, tiles in sorted(scenes.items()):
    xs = sorted(set(t['x'] for t in tiles))
    ys = sorted(set(t['y'] for t in tiles))
    tw = tiles[0]['w']
    th = tiles[0]['h']
    print(f'Scene {s}: {len(tiles)} tiles, tile_w={tw}, tile_h={th}')
    if len(xs) > 1:
        step_x = xs[1] - xs[0]
        ovlp_x = (tw - step_x) / tw
        print(f'  step_x={step_x}, overlap_x={ovlp_x:.4f} ({ovlp_x*100:.1f}%)')
    if len(ys) > 1:
        step_y = ys[1] - ys[0]
        ovlp_y = (th - step_y) / th
        print(f'  step_y={step_y}, overlap_y={ovlp_y:.4f} ({ovlp_y*100:.1f}%)')

# Now analyze new XML to compute tile step from grid geometry
print()
new_xml = Path('00.RawData/MouseTestRawdata20260421/A1-Image Export-01/A1-Image Export-01_meta.xml')
tree2 = ET.parse(new_xml)
root2 = tree2.getroot()

# Get pixel scale
scale_m = None
for d in root2.findall('.//Scaling/Items/Distance'):
    if d.get('Id') == 'X':
        scale_m = float(d.findtext('Value'))

tile_px = 1020  # from tifffile probe
tile_um = tile_px * scale_m * 1e6

print(f'New data pixel scale: {scale_m:.6e} m/px = {scale_m*1e6:.4f} um/px')
print(f'Tile physical size: {tile_um:.2f} um ({tile_px} px)')

for tr in root2.findall('.//TileRegion'):
    name = tr.get('Name')
    center = tr.findtext('CenterPosition')
    size_str = tr.findtext('ContourSize')
    cols = int(tr.findtext('Columns'))
    rows = int(tr.findtext('Rows'))
    size_w, size_h = [float(v) for v in size_str.split(',')]
    step_x_um = size_w / cols
    step_y_um = size_h / rows
    step_x_px = step_x_um / (scale_m * 1e6)
    step_y_px = step_y_um / (scale_m * 1e6)
    ovlp_x = (tile_um - step_x_um) / tile_um
    ovlp_y = (tile_um - step_y_um) / tile_um
    print(f'{name}: {cols}x{rows} tiles, step=({step_x_um:.1f},{step_y_um:.1f}) um = ({step_x_px:.1f},{step_y_px:.1f}) px, overlap=({ovlp_x*100:.1f}%, {ovlp_y*100:.1f}%)')
