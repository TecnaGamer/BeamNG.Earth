"""Vendor exactly the reviewed crates (cpal 0.17.3 and its Linux / Windows dependencies) into a directory source.

- Reviewed crates: taken from cargo's download cache (their tarballs), each tarball's sha256 checked against the
  crates.io index checksum first, extracted unmodified, with the .cargo-checksum.json cargo needs for a directory
  source (the package checksum = the index checksum; every file's sha256).
- Every other package in Cargo.lock (cpal's macOS / iOS / Android / wasm dependencies: nobody reviewed them and our
  builds never compile them, but cargo resolves the whole lock across platforms): a stub with the same name, version
  and feature names, a lib that is a compile_error!, and the crates.io package checksum. A stub can never be built by
  accident: building it fails loudly. This is what plain `cargo vendor` cannot do (it copies every platform's crates).
Nothing is built or run.

usage: python3 -I tools/vendor_reviewed.py CARGO_LOCK VENDOR_DIR
"""
import glob
import hashlib
import io
import json
import os
import re
import sys
import tarfile

REVIEWED = [
    ('alsa', '0.11.0'), ('alsa-sys', '0.4.0'), ('bitflags', '2.13.2'), ('cfg-if', '1.0.5'), ('cpal', '0.17.3'),
    ('dasp_sample', '0.11.0'), ('libc', '0.2.190'), ('libm', '0.2.16'), ('pkg-config', '0.3.34'),
    ('proc-macro2', '1.0.107'),
    ('quote', '1.0.47'), ('syn', '2.0.119'), ('unicode-ident', '1.0.27'), ('windows', '0.62.2'),
    ('windows-collections', '0.3.2'), ('windows-core', '0.62.2'), ('windows-future', '0.3.2'),
    ('windows-implement', '0.60.2'), ('windows-interface', '0.59.3'), ('windows-link', '0.2.1'),
    ('windows-numerics', '0.3.1'), ('windows-result', '0.4.1'), ('windows-strings', '0.5.1'),
    ('windows-threading', '0.2.1'),
]
CACHE = glob.glob(os.path.expanduser('~/.cargo/registry/cache/index.crates.io-*'))[0]
INDEX = glob.glob(os.path.expanduser('~/.cargo/registry/index/*/.cache'))[0]


def index_entry(name, vers):
    n = name.lower()
    if len(n) <= 2:
        p = os.path.join(INDEX, str(len(n)), n)
    elif len(n) == 3:
        p = os.path.join(INDEX, '3', n[0], n)
    else:
        p = os.path.join(INDEX, n[:2], n[2:4], n)
    for chunk in open(p, 'rb').read().split(b'\x00'):
        chunk = chunk.strip()
        if chunk.startswith(b'{'):
            try:
                j = json.loads(chunk)
            except Exception:
                continue
            if j.get('vers') == vers:
                return j
    raise SystemExit(f'{name} {vers}: not in the index cache')


def write_checksum(dest, files, package):
    with open(os.path.join(dest, '.cargo-checksum.json'), 'w') as f:
        json.dump({'files': files, 'package': package}, f, sort_keys=True)


def vendor_reviewed(out, name, vers):
    tb = os.path.join(CACHE, f'{name}-{vers}.crate')
    data = open(tb, 'rb').read()
    ck = hashlib.sha256(data).hexdigest()
    want = index_entry(name, vers)['cksum']
    if ck != want:
        raise SystemExit(f'{name} {vers}: sha256 {ck} != crates.io {want}')
    dest = os.path.join(out, f'{name}-{vers}')
    prefix = f'{name}-{vers}/'
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as t:
        for m in t.getmembers():
            if not m.name.startswith(prefix) or '..' in m.name.split('/'):
                raise SystemExit(f'{name}: unexpected path {m.name}')
            if m.issym() or m.islnk():
                raise SystemExit(f'{name}: link in the tarball: {m.name}')
            if m.isdir():
                continue
            rel = m.name[len(prefix):]
            body = t.extractfile(m).read()
            p = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'wb') as f:
                f.write(body)
            files[rel] = hashlib.sha256(body).hexdigest()
    write_checksum(dest, files, ck)
    print(f'reviewed {name} {vers}: {len(files)} files, sha256 matches crates.io')


def toml_str(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def vendor_stub(out, name, vers):
    j = index_entry(name, vers)
    feats = dict(j.get('features', {}))
    feats.update(j.get('features2', {}) or {})
    # optional dependencies are features too (implicitly, unless named only through dep:)
    for d in j.get('deps', []):
        if d.get('optional') and d['name'] not in feats:
            feats[d['name']] = []
    dest = os.path.join(out, f'{name}-{vers}')
    os.makedirs(os.path.join(dest, 'src'), exist_ok=True)
    lines = [
        '# STUB written by native/weathersynth/tools/vendor_reviewed.py: not the real crate. cpal needs it only on',
        '# platforms we do not build for (macOS, iOS, Android, wasm); it was never reviewed and must never be compiled.',
        '[package]', f'name = {toml_str(name)}', f'version = {toml_str(vers)}', 'edition = "2021"',
        '', '[lib]', 'path = "src/lib.rs"', '', '[features]',
    ]
    for f in sorted(feats):
        lines.append(f'{toml_str(f)} = []')
    files = {}
    body_toml = ('\n'.join(lines) + '\n').encode()
    body_lib = (f'compile_error!("{name} {vers} is a stub in native/weathersynth/vendor: '
                'it is not vendored for this platform");\n').encode()
    for rel, body in (('Cargo.toml', body_toml), ('src/lib.rs', body_lib)):
        with open(os.path.join(dest, rel), 'wb') as f:
            f.write(body)
        files[rel] = hashlib.sha256(body).hexdigest()
    write_checksum(dest, files, j['cksum'])
    print(f'stub     {name} {vers}')


def main():
    lock, out = sys.argv[1], sys.argv[2]
    if os.path.exists(out) and os.listdir(out):
        raise SystemExit(f'{out} is not empty: remove it first')
    os.makedirs(out, exist_ok=True)
    text = open(lock).read()
    pkgs = []
    for blk in text.split('[[package]]')[1:]:
        n = re.search(r'^name = "([^"]+)"', blk, re.M).group(1)
        v = re.search(r'^version = "([^"]+)"', blk, re.M).group(1)
        src = re.search(r'^source = "([^"]+)"', blk, re.M)
        if src and 'crates.io' in src.group(1):
            pkgs.append((n, v))
    reviewed = set(REVIEWED)
    for p in REVIEWED:
        if p not in pkgs:
            raise SystemExit(f'{p} is not in {lock}')
    for n, v in sorted(pkgs):
        if (n, v) in reviewed:
            vendor_reviewed(out, n, v)
        else:
            vendor_stub(out, n, v)


if __name__ == '__main__':
    main()
