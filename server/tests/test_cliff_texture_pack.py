"""The optional pack cannot prevent the bundled cliff materials from working."""
import os as _os, sys as _sys  # (server/ and the repo root, run from anywhere: tools/run_tests.py, unittest)
_sys.path[:0] = [p for p in (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
                 if p not in _sys.path]
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
import clifftextures as ct


class PackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.source = 'levels/beamng_earth/art/shapes/beamngEarth/cliff/canyon_near_base.color.webp'
        self.entries = {'cliff/color.dds': {'source': self.source}}
        stream = io.BytesIO()
        Image.new('RGB', (16, 16), (130, 100, 80)).save(stream, format='WEBP', lossless=True)
        self.body = stream.getvalue()
        self.item = dict(path=ct.pack_path(self.source), size=len(self.body), sha256=hashlib.sha256(self.body).hexdigest())
        self.manifest = dict(format=1, files=[self.item])
        self.calls = []

    def fetch(self, url, **kw):
        self.calls.append(url)
        return SimpleNamespace(body=json.dumps(self.manifest).encode() if url.endswith('manifest.json') else self.body)

    def resolve(self, fetch=None):
        return ct.resolve(self.entries, self.cache, url='https://cdn.example/pack/v1/manifest.json',
                          fetcher=SimpleNamespace(get=fetch or self.fetch), diagnostic=lambda _: None)

    def test_fresh_cached_and_offline(self):
        result = self.resolve()
        self.assertEqual(Path(result[self.source]).read_bytes(), self.body)
        self.calls.clear()
        self.assertEqual(self.resolve(), result)
        self.assertEqual(len(self.calls), 1, 'cached source must not be downloaded again')
        def offline(*a, **kw):
            raise OSError('offline')
        self.assertEqual(self.resolve(offline), result)

    def test_corrupt_download_falls_back_without_publishing(self):
        self.body = b'corrupt'
        self.assertEqual(self.resolve(), {})
        self.assertFalse((self.cache / 'sources').exists())

    def test_corrupt_cache_is_refetched(self):
        result = self.resolve()
        Path(result[self.source]).write_bytes(b'broken')
        self.calls.clear()
        self.assertEqual(self.resolve(), result)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(Path(result[self.source]).read_bytes(), self.body)

    def test_manifest_rejects_paths_sizes_and_duplicates_before_download(self):
        for changes in ({'path': '../escape.webp'}, {'size': -1}, {'size': ct.MAX_SOURCE_BYTES + 1}, {'sha256': 'bad'}):
            self.calls.clear()
            self.manifest['files'] = [dict(self.item, **changes)]
            self.assertEqual(self.resolve(), {})
            self.assertEqual(len(self.calls), 1)
        self.manifest['files'] = [self.item, self.item]
        self.assertEqual(self.resolve(), {})

    def test_unavailable_pack_and_invalid_image_fall_back(self):
        def offline(*a, **kw):
            raise OSError('offline')
        self.assertEqual(self.resolve(offline), {})
        self.body = b'not an image'
        self.item.update(size=len(self.body), sha256=hashlib.sha256(self.body).hexdigest())
        self.assertEqual(self.resolve(), {})

    def test_disabled_and_bundled_choices_do_not_fetch(self):
        entries = {'levels/a/cliff/color.dds': {'source': self.source}}
        with patch.object(ct, 'pack_url', side_effect=AssertionError('CDN discovery attempted')), \
             patch.object(ct, 'resolve', side_effect=AssertionError('download attempted')):
            for env in ({}, {'BNE_CLIFFS': '0'}, {'BNE_CLIFFS': '1', 'BNGEARTH_START_SETTINGS': '{"values":{}}'},
                        {'BNE_CLIFFS': '1', 'BNGEARTH_START_SETTINGS': json.dumps({'values': {
                            'naturalCliffs': False, 'cliffRoadCuts': False, 'cliffTextures': '4k'}})}):
                self.assertEqual(ct.source_overrides(entries, env), {})

    def test_explicit_4k_opt_in(self):
        env = {'BNE_CLIFFS': '1', 'BNGEARTH_START_SETTINGS': '{"values":{"cliffTextures":"4k"}}'}
        entries = {'levels/a/cliff/color.dds': {'source': self.source}}
        with patch.object(ct, 'pack_url', return_value='https://cdn.example/v1/manifest.json'), \
             patch.object(ct, 'resolve', return_value={'source': 'cached'}) as fetch:
            self.assertEqual(ct.source_overrides(entries, env), {'source': 'cached'})
            fetch.assert_called_once()

    def test_discovery_failure_retains_bundled_sources(self):
        env = {'BNE_CLIFFS': '1', 'BNGEARTH_START_SETTINGS': '{"values":{"cliffTextures":"4k"}}'}
        entries = {'levels/a/cliff/color.dds': {'source': self.source}}
        with patch.object(ct, 'pack_url', side_effect=OSError('offline')), \
             patch.object(ct, 'resolve', side_effect=AssertionError('unexpected file request')), \
             patch('builtins.print') as diagnostic:
            self.assertEqual(ct.source_overrides(entries, env), {})
            self.assertIn('using bundled textures', diagnostic.call_args.args[0])

    def test_cdn_switch_selects_version_and_respects_disable(self):
        import cdn
        for entry in (None, {}, {'enabled': False}, {'enabled': True, 'base': 'http://remote/'}):
            with patch.object(cdn, 'switch', return_value={'cliffs': entry}):
                self.assertIsNone(ct.pack_url({}, self.cache, None))
        with patch.object(cdn, 'switch', return_value={'cliffs': {
                'enabled': True, 'base': 'https://cdn.example/cliff/4k/v7/'}}):
            self.assertEqual(ct.pack_url({}, self.cache, None),
                             'https://cdn.example/cliff/4k/v7/manifest.json')

    def test_local_override_does_not_contact_cdn_and_verifies_files(self):
        import cdn
        source = self.cache / 'pack'
        file = source / self.item['path']
        file.parent.mkdir(parents=True)
        file.write_bytes(self.body)
        (source / 'manifest.json').write_text(json.dumps(self.manifest))
        with patch.object(cdn, 'switch', side_effect=AssertionError('unexpected CDN request')):
            url = ct.pack_url({ct.PACK_URL_ENV: str(source)}, self.cache, None)
        result = ct.resolve(self.entries, self.cache / 'download', url=url, local=True,
                            fetcher=SimpleNamespace(get=lambda *a, **kw: self.fail('network request')))
        self.assertEqual(Path(result[self.source]).read_bytes(), self.body)
        self.assertEqual(ct.resolve(self.entries, self.cache / 'blocked', url=url,
                         fetcher=SimpleNamespace(get=self.fetch), diagnostic=lambda _: None), {})

    def test_loopback_http_requires_explicit_override(self):
        url = 'http://127.0.0.1:8123/manifest.json'
        self.assertEqual(ct.resolve(self.entries, self.cache, url=url,
                         fetcher=SimpleNamespace(get=self.fetch), diagnostic=lambda _: None), {})
        result = ct.resolve(self.entries, self.cache, url=url, local=True,
                            fetcher=SimpleNamespace(get=self.fetch))
        self.assertEqual(Path(result[self.source]).read_bytes(), self.body)

class InstallPackTest(unittest.TestCase):
    def test_fresh_cached_quality_change_and_encode_fallback(self):
        import modtextures as mt
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'sources'
            source.mkdir()
            bundled = source / 'color.webp'
            optional = root / 'optional.webp'
            Image.new('RGB', (8, 8), (130, 100, 80)).save(bundled, lossless=True)
            Image.new('RGB', (16, 16), (131, 101, 81)).save(optional, lossless=True)
            rel = 'levels/test/cliff/color.dds'
            entries = {rel: dict(source='color.webp', format='BC7_SRGB', role='color', requires_any=['BNE_CLIFFS'])}
            env = {'BNE_CLIFFS': '1', 'BNGEARTH_START_SETTINGS': '{"values":{}}'}
            target = root / 'installed'
            plan = mt.plan(str(source), entries, env=env)
            self.assertEqual(mt.build(target, plan, threads=1)[:2], (1, 0))
            self.assertEqual(mt.reusable(target, plan), {rel})
            original_bytes = (target / rel).read_bytes()
            with patch.object(ct, 'source_overrides', return_value={'color.webp': str(optional)}):
                higher = mt.plan(str(source), entries, env=env)
            self.assertNotEqual(plan[rel]['key'], higher[rel]['key'])
            self.assertEqual(mt.reusable(target, higher), set())
            self.assertEqual(mt.build(target, higher, threads=1)[:2], (1, 0))
            self.assertNotEqual((target / rel).read_bytes(), original_bytes)
            # A verified downloaded image whose native conversion fails must
            # still install the bundled material, with the bundled cache key.
            with patch.object(ct, 'source_overrides', return_value={'color.webp': str(optional)}):
                retry = mt.plan(str(source), entries, env=env)
            native = mt.native()
            encoder = native.texture_dds_files
            calls = []
            def fail_optional(jobs, threads):
                calls.append(jobs)
                return ['optional conversion failure'] if jobs[0][0] == str(optional) else encoder(jobs, threads)
            with patch.object(mt, 'native', return_value=SimpleNamespace(texture_dds_files=fail_optional)):
                mt.build(target, retry, threads=1)
            self.assertEqual(len(calls), 2)
            self.assertEqual((target / rel).read_bytes(), original_bytes)
            self.assertEqual(mt.reusable(target, plan), {rel})

    def test_both_categories_off_do_not_read_missing_sources(self):
        import modtextures as mt
        env = {'BNE_CLIFFS': '1', 'BNGEARTH_START_SETTINGS': json.dumps({'values': {
            'naturalCliffs': False, 'cliffRoadCuts': False}})}
        self.assertEqual(mt.plan(entries={'levels/test/cliff/missing.dds': {
            'source': 'missing.webp', 'format': 'BC7_SRGB', 'role': 'color'}}, env=env), {})
