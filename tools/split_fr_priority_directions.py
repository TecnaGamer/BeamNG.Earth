"""Split a French priority directions ZIP into cell-aligned ZIPs and a checksum manifest."""

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

MIB = 1024 * 1024


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def split(source, output):
    """Write numbered parts in cell order, preserving each cell's JSON exactly."""
    with zipfile.ZipFile(source) as original:
        index = json.loads(original.read('index.json'))
        if index['format'] != 1:
            raise ValueError('Unsupported French priority direction data format')
        cells = sorted(index['cells'])
        groups, group, size = [], [], 0
        for x, y in cells:
            entry = original.getinfo(f'{x}_{y}.json')
            if group and size + entry.compress_size > 75 * MIB:
                groups.append(group)
                group, size = [], 0
            group.append(((x, y), entry))
            size += entry.compress_size
        if group:
            groups.append(group)
        if not groups:
            raise ValueError('The catalog has no cells')

        files = [output / f'{source.stem}-{n:02d}.zip' for n in range(1, len(groups) + 1)]
        manifest = output / f'{source.stem}.json'
        for path, entries in zip(files, groups):
            with zipfile.ZipFile(path, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                # Counts describe the full source snapshot and belong to the manifest.
                metadata = {k: v for k, v in index.items() if k != 'counts'}
                metadata['cells'] = [cell for cell, _ in entries]
                archive.writestr('index.json', json.dumps(metadata, separators=(',', ':')))
                for _, entry in entries:
                    archive.writestr(entry.filename, original.read(entry))
            if path.stat().st_size >= 100 * MIB:
                raise ValueError(f'Part exceeds the GitHub size limit ({path.name})')

    info = {'source_sha256': sha256(source), 'parts': [sha256(path) for path in files],
            'counts': index.get('counts', {})}
    with manifest.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(info, indent=2) + '\n')
    return [*files, manifest]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for path in split(args.source, args.output):
        print(f'{path.name} ({path.stat().st_size:,} bytes)')
