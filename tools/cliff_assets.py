"""Shared validation for offline cliff material publishing."""

from pathlib import Path
import struct

# Additive micro-roughness must not overwhelm the relief-based main map.
DETAIL_ROUGHNESS_STRENGTH = 0.06


def read_bc5_normal(path, width, height):
    """Read a complete BC5_UNORM DDS, rejecting partial importer output.

    BeamNG may create the cache file before writing all mip levels. Publishing
    only a validated payload keeps packaged normals independent of the importer.
    """
    data = Path(path).read_bytes()
    mips = max(width, height).bit_length()
    payload_size = sum(
        max(1, ((width >> level) + 3) // 4) * max(1, ((height >> level) + 3) // 4) * 16
        for level in range(mips)
    )
    valid = (
        len(data) == 148 + payload_size
        and data[:4] == b'DDS '
        and data[84:88] == b'DX10'
        and struct.unpack_from('<I', data, 128)[0] == 83
        and struct.unpack_from('<II', data, 12) == (height, width)
        and struct.unpack_from('<I', data, 28)[0] == mips
    )
    if not valid:
        raise ValueError(f'Expected complete {width}x{height} BC5_UNORM DDS with {mips} mip levels')
    return data
