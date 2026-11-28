"""Download, verification, offline reuse and failed weekly update recovery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netfetch
import pagesdata
import routerelations


def dataset(root, ref='210'):
    path = Path(root) / 'input.sqlite'
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute('CREATE TABLE routes(id INTEGER PRIMARY KEY,tags TEXT)')
        con.execute('CREATE TABLE members(way_id INTEGER,route_id INTEGER,role TEXT)')
        con.executemany('INSERT INTO routes VALUES (?,?)', [
            (1, json.dumps(dict(type='route', route='road', network='US:WY', ref=ref))),
            (2, json.dumps(dict(type='route', route='road', network='US:US', ref='30')))])
        con.executemany('INSERT INTO members VALUES (?,?,?)', [(10, 1, 'forward'), (10, 2, '')])
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    spec = dict(format=1, built=ref, database_sha256=digest, database_bytes=len(body),
                file=f'route_relations-{digest}.sqlite.gz', data='https://mirror.example/routes')
    return spec, gzip.compress(body)


class Fetcher:
    def __init__(self, spec, archive):
        self.spec, self.archive = spec, archive
        self.calls = []
        self.offline = False
        self.mirror_fails = False

    def get(self, url, **kwargs):
        self.calls.append(url)
        if self.offline or (self.mirror_fails and url.startswith('https://mirror.example')):
            raise OSError('unavailable')
        return SimpleNamespace(body=json.dumps(self.spec).encode() if url.endswith('manifest.json') else self.archive)


class MembershipTest(unittest.TestCase):
    def test_download_join_roles_concurrency_and_offline_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec, body = dataset(tmp)
            fetch = Fetcher(spec, body)
            source = routerelations.Source(tmp, fetch)
            with ThreadPoolExecutor(4) as pool:
                results = list(pool.map(lambda _: source.query([10, 999]), range(4)))
            self.assertTrue(all(r == results[0] for r in results))
            self.assertEqual({r['network'] for r in results[0][10]}, {'US:WY', 'US:US'})
            self.assertEqual(results[0][10][0]['role'], 'forward')
            self.assertEqual(len(fetch.calls), 2)
            fetch.offline = True
            restarted = routerelations.Source(tmp, fetch)
            self.assertEqual(restarted.query([10]), results[0])
            self.assertEqual(len(fetch.calls), 2)
            self.assertEqual(restarted.version(), spec['database_sha256'])

    def test_mirror_failure_uses_pages_and_same_size_cache_corruption_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec, body = dataset(tmp)
            fetch = Fetcher(spec, body)
            fetch.mirror_fails = True
            source = routerelations.Source(tmp, fetch)
            expected = source.query([10])
            self.assertTrue(expected)
            self.assertEqual(len(fetch.calls), 3)
            source.active.write_bytes(b'x' * spec['database_bytes'])
            repaired = routerelations.Source(tmp, fetch)
            self.assertEqual(repaired.query([10]), expected)
            self.assertEqual(hashlib.sha256(repaired.active.read_bytes()).hexdigest(), spec['database_sha256'])

    def test_bad_download_retries_hourly_and_does_not_publish_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec, _ = dataset(tmp)
            fetch = Fetcher(spec, gzip.compress(b'broken'))
            source = routerelations.Source(tmp, fetch)
            self.assertEqual(source.query([10]), {})
            self.assertFalse(source.ready)
            calls = len(fetch.calls)
            self.assertEqual(source.query([10]), {})
            self.assertEqual(len(fetch.calls), calls)
            self.assertGreater(source.retry_at, 0)
            self.assertEqual(list(source.root.glob('*.sqlite')), [])
            self.assertEqual(list(source.root.glob('*.part')), [])

    def test_failed_update_keeps_last_verified_build_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec, body = dataset(tmp)
            fetch = Fetcher(spec, body)
            source = routerelations.Source(tmp, fetch)
            good = source.query([10])
            newer, _ = dataset(tmp, '211')
            fetch.spec, fetch.archive = newer, gzip.compress(b'incomplete')
            source.manifest._take(newer)
            pagesdata.write_atomic(str(source.root / 'manifest.json'), json.dumps(newer).encode())
            self.assertEqual(source.query([10]), good)
            self.assertTrue(source.ready)
            fetch.offline = True
            self.assertEqual(routerelations.Source(tmp, fetch).query([10]), good)

    def test_new_build_invalidates_cached_way_memberships(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec, body = dataset(tmp)
            fetch = Fetcher(spec, body)
            source = routerelations.Source(tmp, fetch)
            self.assertEqual(source.query([10])[10][0]['ref'], '210')
            fetch.spec, fetch.archive = dataset(tmp, '211')
            source.manifest._take(fetch.spec)
            self.assertEqual(source.query([10])[10][0]['ref'], '211')

    def test_manifest_paths_limits_and_sqlite_schema_are_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec, body = dataset(tmp)
            for change in ({'format': 2}, {'file': '../routes.gz'}, {'database_sha256': 'bad'},
                           {'database_bytes': routerelations.MAX_DATABASE_BYTES + 1}, {'database_bytes': True}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    routerelations.validate(dict(spec, **change))
            bad = b'not sqlite'
            digest = hashlib.sha256(bad).hexdigest()
            spec.update(database_sha256=digest, database_bytes=len(bad), file=f'route_relations-{digest}.sqlite.gz')
            source = routerelations.Source(tmp, Fetcher(spec, gzip.compress(bad)))
            self.assertEqual(source.query([10]), {})
            self.assertEqual(list(source.root.glob('*.sqlite')), [])


if __name__ == '__main__':
    unittest.main()
