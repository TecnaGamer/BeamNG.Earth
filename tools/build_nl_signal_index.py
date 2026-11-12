"""Append signal-only pointer tables to the existing offline context pack.

Road records and their original offsets remain unchanged. Re-running produces
identical bytes; old readers can still use the ordinary tile tables.
"""
import hashlib
import json
from pathlib import Path
import re
import struct

ROOT = Path(__file__).resolve().parents[1]


def build(root=ROOT):
    index_path = root / 'mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_groenpol_context_index.lua'
    pack_path = root / 'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/groenpol/context_v1.bin'
    source = index_path.read_text(encoding='utf-8')
    match = re.search(r'\["tiles"\]=\{((?:\["\d+_\d+"\]=\{\d+,\d+\},?)+)\}', source)
    if not match:
        raise ValueError('Missing ordinary tile index')
    slots = re.findall(r'\["(\d+_\d+)"\]=\{(\d+),(\d+)\}', match[1])
    if not slots:
        raise ValueError('Empty ordinary tile index')
    original_end = max(int(at) - 1 + int(count) * 4 for _, at, count in slots)
    pack = bytearray(pack_path.read_bytes()[:original_end])
    if pack[:8] != b'BNECON1\0' or len(pack) != original_end:
        raise ValueError('Invalid context pack')
    signal_slots = []
    for key, at, count in sorted(slots):
        pointers = struct.unpack_from('<' + 'I' * int(count), pack, int(at) - 1)
        signals = [p for p in pointers if pack[p - 1] == 2]
        if signals:
            signal_slots.append(f'["{key}"]={{{len(pack)+1},{len(signals)}}}')
            pack.extend(struct.pack('<' + 'I' * len(signals), *signals))
    source = re.sub(r',\["signal_tiles"\]=\{(?:\["\d+_\d+"\]=\{\d+,\d+\},?)*\}', '', source)
    source = re.sub(r'\["data_bytes"\]=\d+', f'["data_bytes"]={len(pack)}', source)
    digest = hashlib.sha256(pack).hexdigest()
    source = re.sub(r'\["data_sha256"\]="[a-f0-9]+"', f'["data_sha256"]="{digest}"', source)
    end = source.rfind('}')
    source = source[:end] + ',["signal_tiles"]={' + ','.join(signal_slots) + '}' + source[end:]
    pack_path.write_bytes(pack)
    index_path.write_text(source, encoding='utf-8')
    provenance_path = pack_path.with_name('context_provenance.json')
    if provenance_path.exists():
        provenance = json.loads(provenance_path.read_text(encoding='utf-8'))
        provenance.update(data_bytes=len(pack), data_sha256=digest,
                          signal_index={'tiles': len(signal_slots), 'bytes': len(pack)-original_end,
                                        'producer': 'tools/build_nl_signal_index.py'})
        provenance_path.write_text(json.dumps(provenance, indent=2)+'\n', encoding='utf-8')
    print(f'{len(signal_slots)} signal tiles; {len(pack)-original_end} index bytes; SHA256 {digest}')


if __name__ == '__main__':
    build()
