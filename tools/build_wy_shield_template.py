"""Rebuild the numberless WY face. Build-only dependency: pip install resvg-py.

Source: Commons File:WY-210.svg (Vishwin60, public domain, PD MUTCD WY).
The source drawing's path outlines preserve WYOMING and the horse and rider.
"""
import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def main():
    import resvg_py
    from PIL import Image
    ap = argparse.ArgumentParser()
    ap.add_argument('source', type=Path)
    args = ap.parse_args()
    root = ET.parse(args.source).getroot()
    out = ET.Element('svg', xmlns='http://www.w3.org/2000/svg',
                     viewBox='0 0 390.7 393.2', width='512', height='512')
    for node in root:
        if node.get('id') not in ('metadata2410', 'defs2408', 'base', 'text1938'):
            node.tag = node.tag.split('}')[-1]
            out.append(node)
    folder = ROOT/'server/data/shields'
    folder.mkdir(exist_ok=True)
    svg = ET.tostring(out, encoding='unicode')
    (folder/'wy.svg').write_text(svg, encoding='utf-8')
    (folder/'wy.png').write_bytes(resvg_py.svg_to_bytes(svg_string=svg))
    # Small shared digit atlas; no font install or platform font fallback at runtime.
    font = json.loads((ROOT/'server/sign_glyphs.json').read_text(encoding='utf-8'))
    src = Image.open(ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/roadside/sign_letters_o.data.png')
    atlas = Image.new('L', (800, 80))
    glyphs = {}
    x = 0
    for char in '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-':
        g = font['glyphs'][char]
        u0, v0, u1, v1 = g['uv']
        crop = src.crop((round(u0*src.width), round((1-v1)*src.height), round(u1*src.width), round((1-v0)*src.height)))
        # Store each glyph separately; digits and suffixes retain their original advances.
        if x+crop.width > atlas.width:
            enlarged = Image.new('L', (atlas.width*2,80)); enlarged.paste(atlas,(0,0)); atlas=enlarged
        atlas.paste(crop, (x,0))
        glyphs[char] = dict(box=[x,0,x+crop.width,crop.height], advance=g['advance'], bbox=g['bbox'])
        x += crop.width+2
    atlas.crop((0,0,x,80)).save(folder/'lettering.png')
    (folder/'lettering.json').write_text(json.dumps(dict(cap=font['capHeight'], glyphs=glyphs)), encoding='utf-8')


if __name__ == '__main__':
    main()
