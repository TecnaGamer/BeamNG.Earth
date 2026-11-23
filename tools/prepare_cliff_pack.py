"""Export original cliff WebPs, then derive the bundled 2K/1K fallback maps.

Run on the authored sources: python tools/prepare_cliff_pack.py build/cliff-4k --originals SOURCE_DIR
On later runs the exported originals are reused, never upscaled from fallbacks.
The output is a local handoff directory; publication is a separate action.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clifftextures import pack_path


def resize(source, role):
    with Image.open(source) as image:
        image = image.convert('RGB' if role in ('color', 'normal') else 'L')
    limit = 2048 if role == 'color' else 1024
    if max(image.size) <= limit:
        return image
    size = tuple(max(1, round(n * limit / max(image.size))) for n in image.size)
    if role == 'normal':
        normal = np.asarray(image, dtype=np.float32) / 127.5 - 1
        normal = np.stack([np.asarray(Image.fromarray(normal[..., c]).resize(size, Image.Resampling.LANCZOS))
                           for c in range(3)], axis=-1)
        normal /= np.maximum(np.linalg.norm(normal, axis=-1, keepdims=True), 1e-8)
        return Image.fromarray(np.clip(np.rint((normal + 1) * 127.5), 0, 255).astype(np.uint8))
    return image.resize(size, Image.Resampling.BOX if role == "data" else Image.Resampling.LANCZOS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--originals', type=Path, help='authored directory using the same <set>/<name>.webp layout')
    args = parser.parse_args()
    sources = ROOT / 'mod/texture_sources'
    entries = json.loads((sources / 'textures.json').read_text())['textures']
    # Validate the entire handoff before changing any bundled file. A fresh clone
    # contains fallbacks, not originals; never relabel its 2K maps as a 4K pack.
    for rel, entry in entries.items():
        if '/cliff/' not in rel:
            continue
        exported = args.output / pack_path(entry['source'])
        original = args.originals / pack_path(entry['source']) if args.originals else exported
        if not exported.is_file() and not original.is_file():
            parser.error(f'authored original required: {original}')
        with Image.open(exported if exported.is_file() else original) as image:
            if entry['role'] == 'color' and image.size != (4096, 4096):
                parser.error(f'4K authored color original required: {original}')
    files, bundled = [], 0
    for rel, entry in entries.items():
        if '/cliff/' not in rel:
            continue
        src = sources / entry['source']
        exported = args.output / pack_path(entry['source'])
        exported.parent.mkdir(parents=True, exist_ok=True)
        if not exported.exists():
            shutil.copyfile(args.originals / pack_path(entry['source']), exported)
        with Image.open(exported) as im:
            original_size = im.size
        files.append(dict(path=pack_path(entry['source']), size=exported.stat().st_size,
                          sha256=hashlib.sha256(exported.read_bytes()).hexdigest(), dimensions=original_size))
        resize(exported, entry['role']).save(src, format='WEBP', lossless=True, method=6, exact=True)
        bundled += src.stat().st_size
    # An inventory for handoff/verification, not the maintainer's published manifest.
    (args.output / 'inventory.json').write_text(json.dumps(dict(files=files), indent=2))
    print(json.dumps(dict(files=len(files), original_bytes=sum(f['size'] for f in files),
                          bundled_bytes=bundled), indent=2))


if __name__ == '__main__':
    main()
