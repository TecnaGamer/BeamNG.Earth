#!/usr/bin/env python3
"""Capture or replay one production boundary using a trusted local Python adapter.

capture(record, cache) drives the real caller and calls record(label, inputs, output).
For simple hooks use record.call(label, original, *args, **kwargs); its invoke receives
(args, kwargs). Mutating calls can record observable state alongside their result.
invoke(inputs, cache) calls the candidate and returns the same observable result.
The adapter owns binding-specific conversions, callback transcripts and coverage assertions.
Use plain JSON scalars, ordered lists/tuples/dicts, numeric NumPy scalars/arrays and bytes.
Array values preserve dtype, shape and C-order bytes. Describe aliases or strides in the
adapter when observable. --case LABEL replays just the first failing call.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

import pcommon


def canonical(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pack(value, arrays, blobs):
    import numpy as np
    kind = type(value)
    if kind in (bool, int, str) or value is None:
        return value
    if kind is float:
        return ['f64', struct.pack('>d', value).hex()]
    if kind is bytes:
        key = digest(value)
        blobs.setdefault(key, value)
        return ['bytes', key]
    if kind is np.ndarray or isinstance(value, np.generic):
        arr = np.asarray(value)
        if arr.dtype.kind not in 'biufcSUMm':
            raise TypeError(f'unsupported replay dtype {arr.dtype}')
        key = digest(canonical([arr.dtype.str, arr.shape]) + arr.tobytes())
        if key not in arrays:
            arrays[key] = arr.copy(order='C')  # own the input even if the real call mutates it
        return ['array' if kind is np.ndarray else 'scalar', key]
    if kind in (tuple, list):
        return [kind.__name__, [pack(v, arrays, blobs) for v in value]]
    if kind is dict:
        return ['dict', [[pack(k, arrays, blobs), pack(v, arrays, blobs)] for k, v in value.items()]]
    raise TypeError(f'freeze {kind.__name__} explicitly in the adapter')


def unpack(node, arrays, folder):
    if not isinstance(node, list):
        return node
    kind, data = node
    if kind == 'f64':
        return struct.unpack('>d', bytes.fromhex(data))[0]
    if kind in ('bytes', 'array', 'scalar'):
        if len(data) != 64 or any(c not in '0123456789abcdef' for c in data):
            raise ValueError('invalid content address')
        if kind == 'bytes':
            value = (folder / (data + '.bin')).read_bytes()
            key = digest(value)
        else:
            value = arrays[data]
            key = digest(canonical([value.dtype.str, value.shape]) + value.tobytes())
        if key != data:
            raise ValueError(f'damaged fixture payload {data}')
        return value[()] if kind == 'scalar' else value
    if kind == 'dict':
        return {unpack(k, arrays, folder): unpack(v, arrays, folder) for k, v in data}
    if kind in ('list', 'tuple'):
        values = [unpack(v, arrays, folder) for v in data]
        return tuple(values) if kind == 'tuple' else values
    raise ValueError(f'unknown replay type {kind}')


def difference(a, b, path='$'):
    """First observable divergence, including signed zero, NaN payloads and mapping order."""
    import numpy as np
    if type(a) is not type(b):
        return f'{path} type {type(a).__name__} != {type(b).__name__}'
    if isinstance(a, (np.ndarray, np.generic)):
        if a.dtype != b.dtype or a.shape != b.shape:
            return f'{path} {a.dtype}{a.shape} != {b.dtype}{b.shape}'
        x, y = a.tobytes(), b.tobytes()
        if x != y:
            offset = pcommon._first_diff(x, y)
            index = tuple(map(int, np.unravel_index(offset // a.itemsize, a.shape)))
            return f'{path}{index} byte {offset % a.itemsize}: {x[offset]:02x} != {y[offset]:02x}'
    elif isinstance(a, bytes):
        if a != b:
            i = pcommon._first_diff(a, b)
            return f'{path} byte {i}: {a[i:i+1].hex() or "EOF"} != {b[i:i+1].hex() or "EOF"}'
    elif isinstance(a, dict):
        for i, (k, other) in enumerate(zip(a, b)):
            err = difference(k, other, f'{path}.keys[{i}]') or difference(a[k], b[other], f'{path}[{k!r}]')
            if err:
                return err
        if len(a) != len(b):
            return f'{path} key count {len(a)} != {len(b)}'
    elif isinstance(a, (list, tuple)):
        for i, (x, y) in enumerate(zip(a, b)):
            err = difference(x, y, f'{path}[{i}]')
            if err:
                return err
        if len(a) != len(b):
            return f'{path}[{min(len(a), len(b))}] length {len(a)} != {len(b)}'
    elif isinstance(a, float):
        if struct.pack('>d', a) != struct.pack('>d', b):
            return f'{path} {a!r} != {b!r} ({struct.pack(">d", a).hex()} != {struct.pack(">d", b).hex()})'
    elif a != b:
        return f'{path} {a!r} != {b!r}'
    return None


class Recorder:
    def __init__(self, cases):
        self.cases = cases
        self.inventory = {}

    def call(self, label, original, *args, **kwargs):
        """Snapshot the actual arguments before calling a possibly mutating production function."""
        arrays, blobs = {}, {}
        inputs = pack((args, kwargs), arrays, blobs)
        result = original(*args, **kwargs)
        self.save(label, inputs, result, arrays, blobs)
        return result

    def __call__(self, label, inputs, output):
        arrays, blobs = {}, {}
        self.save(label, pack(inputs, arrays, blobs), output, arrays, blobs)

    def save(self, label, inputs, output, arrays, blobs):
        import numpy as np
        if not isinstance(label, str) or label in self.inventory:
            raise ValueError(f'invalid or duplicate case {label!r}')
        folder = self.cases / str(len(self.inventory))
        folder.mkdir()
        manifest = canonical({'input': inputs, 'output': pack(output, arrays, blobs)})
        (folder / 'call.json').write_bytes(manifest)
        np.savez(folder / 'arrays.npz', **arrays)
        for name, blob in blobs.items():
            (folder / (name + '.bin')).write_bytes(blob)
        self.inventory[label] = [digest(manifest), digest(canonical(inputs))]


def capture(adapter, cases, cache):
    cases.mkdir()  # exclusive: never replace an oracle, even after interrupted capture
    record = Recorder(cases)
    adapter.capture(record, cache)
    if not record.inventory:
        raise ValueError('capture observed zero calls')
    (cases / 'manifest.json').write_bytes(canonical({'version': 1, 'calls': record.inventory}))
    print(f'captured {len(record.inventory)} calls')


def replay(adapter, cases, cache, selected):
    import numpy as np
    manifest = json.loads((cases / 'manifest.json').read_bytes())
    calls = manifest['calls']
    if manifest['version'] != 1 or not calls or selected is not None and selected not in calls:
        raise ValueError('unknown fixture version, empty inventory or missing selected case')
    if {p.name for p in cases.iterdir() if p.is_dir()} != {str(i) for i in range(len(calls))}:
        raise ValueError('case folders differ from manifest inventory')
    for i, (label, (checksum, input_sha)) in enumerate(calls.items()):
        if selected is not None and selected != label:
            continue
        folder = cases / str(i)
        raw = (folder / 'call.json').read_bytes()
        call = json.loads(raw)
        if digest(raw) != checksum or digest(canonical(call['input'])) != input_sha:
            raise ValueError(f'damaged case {label}')
        context = f'{label!r} input={input_sha} file={folder / "call.json"}'
        with np.load(folder / 'arrays.npz', allow_pickle=False) as arrays:
            inputs = unpack(call['input'], arrays, folder)
            expected = unpack(call['output'], arrays, folder)
        try:
            actual = adapter.invoke(inputs, cache)
        except Exception as exc:
            raise RuntimeError(f'{context}: candidate raised {exc!r}') from exc
        err = difference(expected, actual)
        if err:
            raise SystemExit(f'{context}: {err}')
    print(f'{1 if selected is not None else len(calls)} identical calls')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['capture', 'replay'])
    for arg in ('adapter', 'server-dir', 'cache', 'cases', 'work'):
        parser.add_argument('--' + arg, type=Path, required=True)
    parser.add_argument('--case')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    a = parser.parse_args()
    if not a.worker:
        if a.case is not None and a.mode != 'replay':
            parser.error('--case selects one existing capture for replay')
        if a.mode == 'capture' and a.cases.exists():
            parser.error(f'capture requires a new cases directory: {a.cases}')
        pcommon.check_apart(a.cache, a.work)
        pcommon.check_apart(a.cache, a.cases)
        pcommon.check_cache(a.work)
        if a.mode == 'capture':
            pcommon.check_cache(a.cases)
        a.work = a.work.resolve()
        a.work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=a.work, prefix='replay-') as tmp:
            cache = pcommon.work_copy(a.cache, os.path.join(tmp, 'cache'))
            cmd = [sys.executable, str(Path(__file__).resolve()), a.mode, '--worker', '--cache', cache,
                   '--adapter', str(a.adapter.resolve()), '--server-dir', str(a.server_dir.resolve()),
                   '--cases', str(a.cases.resolve()), '--work', str(a.work.resolve())]
            if a.case is not None:
                cmd += ['--case', a.case]
            env = dict(os.environ, **pcommon.PINNED_ENV, BNGE_CACHE=cache, BNGE_CACHE_DIR=cache, TMPDIR=tmp,
                       PYTHONHASHSEED=os.environ.get('PYTHONHASHSEED', '0'), PYTHONDONTWRITEBYTECODE='1')
            env.pop('PYTHONPATH', None)
            return subprocess.run(cmd, env=env, cwd=tmp).returncode
    if a.cache.name != 'cache' or not a.cache.parent.name.startswith('replay-') or a.cache.resolve().parent.parent != a.work.resolve():
        parser.error('worker requires its private cache under --work')
    sys.path.insert(0, str(a.server_dir.resolve()))
    import npyguard
    npyguard.apply()
    spec = importlib.util.spec_from_file_location('port_adapter', a.adapter)
    adapter = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = adapter
    sys.path.insert(1, str(a.adapter.resolve().parent))
    spec.loader.exec_module(adapter)
    if a.mode == 'capture':
        capture(adapter, a.cases, a.cache)
    else:
        replay(adapter, a.cases, a.cache, a.case)


if __name__ == '__main__':
    sys.exit(main())
