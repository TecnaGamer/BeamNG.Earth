"""Optional authored cliff sources. Network and conversion happen only at startup.

The bundled sources always remain usable. The CDN URL is supplied by the
maintainer after publication; no guessed production endpoint is contacted.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

PACK_URL_ENV = 'BNE_CLIFFS_PACK_URL'
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_PACK_BYTES = 512 * 1024 * 1024


def pack_url(env, cache, fetcher):
    """Discover the published pack, or an explicitly selected local test pack."""
    override = env.get(PACK_URL_ENV, '').strip()
    if override:
        # Windows drive letters are parsed as schemes; recognize paths first.
        path = Path(override)
        base = path.resolve().as_uri() if path.is_absolute() or not urlsplit(override).scheme else override
    else:
        import cdn
        entry = cdn.switch(fetcher, str(cache)).get('cliffs')
        if not isinstance(entry, dict) or entry.get('enabled') is not True:
            return None
        base = entry.get('base')
        if not isinstance(base, str) or urlsplit(base).scheme != 'https':
            return None
    return base.rstrip('/') + '/manifest.json'


def _read(url, fetcher, limit, local=False):
    """Bound local test reads; all HTTP traffic uses the shared download system."""
    parts = urlsplit(url)
    if local and parts.scheme == 'file' and not parts.netloc:
        with Path(url2pathname(parts.path)).open('rb') as stream:
            body = stream.read(limit + 1)
        if len(body) > limit:
            raise ValueError('local pack file exceeds size limit')
        return body
    if parts.scheme != 'https' and not (
            local and parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('texture URLs require HTTPS (local overrides also allow files and loopback HTTP)')
    return fetcher.get(url, kind='demand', max_bytes=limit).body


def pack_path(source):
    name = Path(source).name
    family = name.split('_', 1)[0]
    return f"{family if family in ('natural', 'limestone', 'arid', 'canyon') else 'shared'}/{name}"


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        f.write(data)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _verified(path, size, digest):
    try:
        return path.stat().st_size == size and hashlib.sha256(path.read_bytes()).hexdigest() == digest
    except OSError:
        return False


def resolve(entries, cache, *, url=None, fetcher=None, diagnostic=print, local=False):
    """Return verified optional sources by bundled source path, or bundled fallbacks.

    Only exact allowlisted filenames can be downloaded. A complete manifest is
    validated before file requests; one bad file cannot discard other valid maps.
    """
    if not url:
        diagnostic('Cliffs: 4K pack is not published yet; using bundled textures.')
        return {}
    allowed = {pack_path(e['source']): e['source'] for e in entries.values()}
    cache = Path(cache)
    manifest_cache = cache / (hashlib.sha256(url.encode()).hexdigest() + '.json')
    try:
        if fetcher is None:
            import netfetch
            fetcher = netfetch.default_fetcher()
        try:
            raw = _read(url, fetcher, MAX_MANIFEST_BYTES, local)
        except Exception:
            raw = manifest_cache.read_bytes()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError('manifest exceeds size limit')
        manifest = json.loads(raw)
        if manifest.get('format') != 1 or not isinstance(manifest.get('files'), list):
            raise ValueError('unsupported texture manifest')
        files, total = {}, 0
        for item in manifest['files']:
            name, size, digest = item['path'], item['size'], item['sha256']
            if name not in allowed or name in files or type(size) is not int or not 0 < size <= MAX_SOURCE_BYTES:
                raise ValueError('invalid texture file entry')
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('invalid texture digest')
            total += size
            if total > MAX_PACK_BYTES:
                raise ValueError('texture pack exceeds size limit')
            files[name] = (size, digest)
        _atomic(manifest_cache, raw)
    except Exception as exc:
        diagnostic(f'Cliffs: optional texture pack unavailable ({exc}); using bundled textures.')
        return {}
    result = {}
    for name, (size, digest) in files.items():
        path = cache / 'sources' / (digest + '.webp')
        try:
            if not _verified(path, size, digest):
                body = _read(urljoin(url, name), fetcher, size, local)
                if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
                    raise ValueError('texture verification failed')
                _atomic(path, body)
            from PIL import Image
            with Image.open(path) as image:
                if (image.format != 'WEBP' or max(image.size) > 4096
                        or any(n & (n - 1) for n in image.size) or getattr(image, 'n_frames', 1) != 1):
                    raise ValueError('unsupported texture image')
                image.load()
            result[allowed[name]] = str(path)
        except Exception as exc:
            diagnostic(f'Cliffs: using bundled {name} ({exc}).')
    return result


def source_overrides(entries, env):
    """Resolve only an explicit 4K choice behind the master opt-in."""
    from cliffsettings import CliffSettings
    settings = CliffSettings.from_env(env)
    if not settings.enabled or settings.textures != '4k':
        return {}
    entries = {p: e for p, e in entries.items() if '/cliff/' in p}
    if not entries:
        return {}
    import apppaths
    cache = Path(env.get('BNGE_CACHE_DIR') or apppaths.app_settings().get('cacheDir')
                 or apppaths.default_cache_dir())
    try:
        import netfetch
        fetcher = netfetch.default_fetcher()
        url = pack_url(env, cache, fetcher)
        return resolve(entries, cache / 'cliff-textures-v1', url=url, fetcher=fetcher,
                       local=bool(env.get(PACK_URL_ENV, '').strip()))
    except Exception as exc:
        print(f'Cliffs: optional pack discovery failed ({exc}); using bundled textures.')
        return {}


def prepare(user_dir, env, diagnostic=print):
    """Apply only authored cliff textures on a settings restart, retaining other stamps."""
    import modtextures
    from cliffsettings import CliffSettings
    env = dict(env, BNGE_USER_DIR=str(user_dir))
    if not CliffSettings.from_env(env).enabled:
        return
    entries = {p: e for p, e in modtextures.manifest().items() if '/cliff/' in p}
    dest = Path(user_dir) / 'mods/unpacked/beamng_earth'
    if not dest.is_dir():
        return
    old_stamp = modtextures.read_stamp(dest)
    todo = modtextures.plan(entries=entries, env=env)
    keep = modtextures.reusable(dest, todo)
    if len(keep) == len(todo):
        return
    diagnostic('Cliffs: preparing selected texture quality before starting the world server.')
    modtextures.build(dest, todo, keep)
    stamp = {p: e for p, e in old_stamp.items() if p not in todo}
    stamp.update(modtextures.read_stamp(dest))
    _atomic(dest / modtextures.STAMP, json.dumps(stamp, sort_keys=True).encode())
