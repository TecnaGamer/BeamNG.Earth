"""Pinned road plans: bounded untrusted bytes, immutable identity and actual grader consumption."""
import asyncio
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worldbundle as wb
import plancache
import grading
import geodesy
import revisitpack
from test_grading import FakeMD
from test_roads import features, FRAME, LAT, LON

KEY = (14, 4000, 6000)
DIGEST = "a" * 64


class BundleTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(revisitpack.reset)
        self.root = Path(temp.name)
        self.pc = plancache.PlanCache(str(self.root / "plans"), "w0s0")
        self.bundle = self.root / "bundle"
        self.env = mock.patch.dict(os.environ, {plancache.RUN_ENV: "test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(wb.ENV, None)

    def plan(self):
        p = grading.Plan(np.array([[0., 0.], [10., 0.], [20., 1.]]), np.array([3., 3.1, 3.2]), 3.)
        p.cut = np.array([[1., 2., 3., np.nan, np.nan, np.nan], [1., 2., 3., 4., 5., 6.]])
        p.bank = np.array([0., .1, .2])
        return p

    def write(self, complete=True):
        self.assertTrue(self.pc.store(KEY, [("road", self.plan())], [], [], [], persist=complete))
        return Path(self.pc._path(KEY))

    def export(self):
        return wb.build_bundle(self.pc.dir, self.bundle, {}, DIGEST, ["Synthetic test fixture"])

    def arrays(self):
        path = self.write()
        with np.load(path, allow_pickle=False) as data:
            return {name: data[name] for name in data.files}

    def encoded(self, arrays):
        out = io.BytesIO()
        np.savez(out, **arrays)
        return out.getvalue()

    def test_export_roundtrip_and_strict_runtime_pin(self):
        self.write()
        manifest = self.export()
        self.assertEqual(manifest, wb.validate_bundle(self.bundle, manifest["id"]))
        pc = wb.PinnedPlanCache(self.bundle, "w0s0", {})
        state, mine = pc.claim(KEY)
        self.assertFalse(mine)
        np.testing.assert_array_equal(state[0][0][1].xy, self.plan().xy)
        np.testing.assert_array_equal(state[0][0][1].cut, self.plan().cut)
        np.testing.assert_array_equal(state[0][0][1].bank, self.plan().bank)
        self.assertEqual(pc.stats["read"], 1)
        with self.assertRaises(wb.BundleError):
            pc.store(KEY, [], [], [], [])
        with self.assertRaises(wb.BundleError):
            pc.claim((14, 4001, 6000))

    def test_export_rejects_incomplete_historical_and_stale_markers(self):
        p = self.write(False)
        with self.assertRaises(wb.BundleError):
            self.export()
        self.write(True)
        p.write_bytes(p.read_bytes() + b"changed")
        with self.assertRaises(wb.BundleError):
            self.export()
        self.write(True)
        self.write(False)
        self.assertFalse(Path(str(p) + ".complete").exists())
        with self.assertRaises(wb.BundleError):
            self.export()

    def test_complete_marker_cannot_bless_an_intervening_writer(self):
        p = Path(self.pc._path(KEY))
        original = os.replace
        def racing(src, dest):
            original(src, dest)
            if dest == str(p):
                p.write_bytes(b"incomplete concurrent result")
        with mock.patch.object(plancache.os, "replace", side_effect=racing):
            self.write(True)
        with self.assertRaises(wb.BundleError):
            self.export()

    def test_export_rejects_relabeling_generation_settings_or_code(self):
        with mock.patch.dict(os.environ, {"BNE_GRADE_MAJOR": "40"}):
            self.write()
        with mock.patch.dict(os.environ, {"BNE_GRADE_MAJOR": "80"}):
            with self.assertRaises(wb.BundleError):
                self.export()
        with mock.patch.object(wb, "generator_identity", return_value={"code": "older-generator", "native": 116}):
            self.write()
        with self.assertRaises(wb.BundleError):
            self.export()
        self.assertFalse(self.bundle.exists())

    def test_provenance_binds_cell_sources_and_rejects_legacy_markers(self):
        path = self.write()
        marker = Path(str(path) + ".complete")
        original = marker.read_bytes()
        marker.write_text(hashlib.sha256(path.read_bytes()).hexdigest(), encoding="ascii")
        with self.assertRaises(wb.BundleError):
            self.export()
        marker.write_bytes(original)
        with self.assertRaises(wb.BundleError):
            wb.build_bundle(self.pc.dir, self.bundle, {"rails": "invented"}, DIGEST, ["Synthetic test fixture"])
        renamed = path.with_name("14_4001_6000.npz")
        path.rename(renamed)
        marker.rename(Path(str(renamed) + ".complete"))
        with self.assertRaises(wb.BundleError):
            self.export()

    def test_source_provenance_is_resolved_after_generation(self):
        sources = {"rails": None}
        md = SimpleNamespace(versions=lambda: dict(sources), bundle_versions=lambda: dict(sources))
        self.pc = plancache.for_grader(str(self.root), SimpleNamespace(md=md), None, None)
        sources["rails"] = "release-discovered-during-generation"
        path = self.write()
        record = json.loads(Path(str(path) + ".complete").read_bytes())
        self.assertEqual(record["sources"], sources)
        manifest = wb.build_bundle(self.pc.dir, self.bundle, sources, DIGEST, ["Synthetic test fixture"])
        self.assertEqual(manifest["sources"], sources)

    def test_cached_source_versions_are_offline_and_used_by_pin_setup(self):
        import mapdata
        import netfetch
        import railtags
        for name in ("rail", "lanes"):
            path = self.root / "pagesdata" / name / "manifest.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"built": "source-build-1"}), encoding="utf-8")
        net = netfetch.Fetcher(offline=True)
        self.addCleanup(net.close)
        with mock.patch.object(railtags, "ON", True), mock.patch.object(railtags, "LANES_ON", True):
            md = mapdata.MapData(str(self.root), fetcher=net)
            missing = mapdata.MapData(str(self.root / "empty-cache"), fetcher=net)
        self.addCleanup(md.close)
        self.addCleanup(missing.close)
        with mock.patch.object(net, "get", side_effect=AssertionError("No source download allowed")) as get:
            # (the pinned build, read from disk without a download: the tiles' cache keys the same from a run's start,
            # railtags.Shards.known, 2026-11-04)
            self.assertEqual(md.versions()["rails"], "source-build-1")
            sources = md.bundle_versions()
            self.assertEqual(sources["rails"], "source-build-1")
            self.assertEqual(sources["lanes"], "source-build-1")
            self.pc.source_versions = md.bundle_versions
            self.write()
            wb.build_bundle(self.pc.dir, self.bundle, sources, DIGEST, ["Synthetic test fixture"])
            os.environ[wb.ENV] = str(self.bundle)
            pinned = plancache.for_grader(str(self.root), SimpleNamespace(md=md), None, None)
            self.assertIsNotNone(pinned.claim(KEY)[0])
            with self.assertRaises(wb.BundleError):
                plancache.for_grader(str(self.root), SimpleNamespace(md=missing), None, None)
            get.assert_not_called()
        # An already used build remains the identity even if the disk manifest changes later.
        md.rail_tags.built = "actively-used-build"
        self.assertEqual(md.bundle_versions()["rails"], "actively-used-build")

    def test_cached_source_manifest_invalid_values_fail_closed(self):
        path = self.root / "source-manifest.json"
        self.assertIsNone(wb.cached_source_build(path))
        for data in (b'{}', b'{"built":null}', b'{"built":2}', b'{"built":""}',
                     b'{"built":"a","built":"b"}', b'{' , b' ' * (wb.MAX_MANIFEST_BYTES + 1)):
            path.write_bytes(data)
            with self.subTest(data=data[:40]), self.assertRaises(wb.BundleError):
                wb.cached_source_build(path)

    def test_unavailable_provenance_does_not_disable_local_plan_cache(self):
        path = self.write()
        self.pc.source_versions = mock.Mock(side_effect=wb.BundleError("Corrupt source metadata"))
        self.write()
        self.assertIsNotNone(self.pc.load(KEY))
        self.assertFalse(Path(str(path) + ".complete").exists())
        with self.assertRaises(wb.BundleError):
            self.export()

    def test_failed_pin_startup_does_not_leave_services_running(self):
        import earthd
        os.environ[wb.ENV] = str(self.bundle)
        early = SimpleNamespace(roads="on", grading="on", peers="")
        with mock.patch.object(earthd.netfetch, "Fetcher") as fetcher, \
                mock.patch.object(earthd.cachemod, "Cache") as cache:
            for _ in range(2):
                with self.assertRaises(OSError):
                    asyncio.run(earthd.main_async(early))
            fetcher.assert_not_called()
            cache.assert_not_called()
        self.write()
        self.export()
        args = SimpleNamespace(**vars(early), far_format="dae", lod_k=None, near_radii=None,
                               cache_dir=str(self.root), offline=True, surface="off", sea="off", trees="off",
                               road_widths="off")
        md = mock.Mock()
        md.bundle_versions.return_value = {"rails": "incompatible"}
        with mock.patch.object(earthd.netfetch, "Fetcher") as fetcher, \
                mock.patch.object(earthd.mapdatamod, "MapData", return_value=md), \
                mock.patch.object(earthd.cachemod, "Cache") as cache:
            for _ in range(2):
                with self.assertRaises(wb.BundleError):
                    asyncio.run(earthd.main_async(args))
            self.assertEqual(md.close.call_count, 2)
            self.assertEqual(fetcher.return_value.close.call_count, 2)
            cache.assert_not_called()
            md.close.side_effect = RuntimeError("reader close failed")
            with self.assertRaisesRegex(RuntimeError, "reader close failed"):
                asyncio.run(earthd.main_async(args))
            self.assertEqual(fetcher.return_value.close.call_count, 3)

    def test_identity_is_canonical_and_immutable(self):
        self.write()
        manifest = self.export()
        self.assertEqual(wb.manifest_id(dict(reversed(list(manifest.items())))), manifest["id"])
        with self.assertRaises(wb.BundleError):
            self.export()
        for field, value in (("signature", "w1s0"), ("input_digest", "b" * 64), ("sources", {"release": "next"})):
            changed = dict(manifest, **{field: value})
            with self.assertRaises(wb.BundleError):
                wb.validate_manifest(changed)
        with self.assertRaises(wb.BundleError):
            wb.validate_manifest(manifest, "0" * 64)

    def test_source_identity_normalizes_checkout_line_endings(self):
        import peerwork
        a, b = self.root / "lf", self.root / "crlf"
        a.mkdir()
        b.mkdir()
        (a / "generator.py").write_bytes(b"x = 1\ny = 2\n")
        (b / "generator.py").write_bytes(b"x = 1\r\ny = 2\r\n")
        self.assertEqual(peerwork.source_id(a), peerwork.source_id(b))
        before = wb.generator_identity()
        with mock.patch.dict(os.environ, {"BNE_ROADZ": "off"}):
            self.assertNotEqual(before, wb.generator_identity())

    def test_bridge_identity_uses_the_effective_profile(self):
        import roads
        with mock.patch.dict(os.environ):
            os.environ.pop("BNE_BRIDGE_PROFILE", None)
            default = wb.generator_identity()
            for value in ("curve", "CURVE", "flat", ""):
                # The existing generator treats any non-legacy string as curve; flat is not a new mode.
                os.environ["BNE_BRIDGE_PROFILE"] = value
                self.assertEqual(roads.bridge_profile_mode(), "curve")
                self.assertEqual(wb.generator_identity(), default)
            os.environ["BNE_BRIDGE_PROFILE"] = "legacy"
            legacy = wb.generator_identity()
            self.assertNotEqual(legacy, default)
            os.environ["BNE_BRIDGE_PROFILE"] = "LEGACY"
            self.assertEqual(wb.generator_identity(), legacy)
            self.write()
            os.environ.pop("BNE_BRIDGE_PROFILE")
            with self.assertRaises(wb.BundleError):
                self.export()

    def test_default_producer_plans_export_in_a_clean_process(self):
        root = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        env.pop("BNE_BRIDGE_PROFILE", None)
        env.pop(wb.ENV, None)
        producer = """import os, sys
sys.path.insert(0, 'server')
import plancache
# earthd.main sets its parsed default before generating plans.
os.environ['BNE_BRIDGE_PROFILE'] = 'curve'
cache = plancache.PlanCache(sys.argv[1], 'w0s0')
assert cache.store((14, 4000, 6000), [], [], [], [], persist=True)
"""
        plans = self.root / "producer"
        result = subprocess.run([sys.executable, "-c", producer, str(plans)], cwd=root, env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        sources = self.root / "sources.json"
        sources.write_text("{}", encoding="utf-8")
        result = subprocess.run([sys.executable, str(root / "tools" / "world_cache.py"), "export",
                                 str(plans / "r10" / "w0s0"), str(self.bundle), "--sources", str(sources),
                                 "--input-digest", DIGEST, "--credit", "Synthetic test fixture"], cwd=root,
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = wb.validate_bundle(self.bundle, json.loads(result.stdout)["manifest_id"])
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(manifest["generator"], wb.generator_identity())
            self.assertIsNotNone(wb.PinnedPlanCache(self.bundle, "w0s0", {}).claim(KEY)[0])

    def test_manifest_shape_limits_and_paths(self):
        self.write()
        manifest = self.export()
        variants = []
        for field, value in (("schema", 2), ("signature", "../escape"), ("attribution", []), ("input_digest", "bad")):
            variants.append(dict(manifest, **{field: value}))
        changed = copy.deepcopy(manifest)
        changed["artifacts"][0]["sha256"] = "../escape"
        variants.append(changed)
        changed = copy.deepcopy(manifest)
        changed["artifacts"] *= 2
        variants.append(changed)
        changed = copy.deepcopy(manifest)
        changed["artifacts"][0]["size"] = wb.MAX_ARTIFACT_BYTES + 1
        variants.append(changed)
        for changed in variants:
            changed["id"] = wb.manifest_id(changed)
            with self.assertRaises(wb.BundleError):
                wb.validate_manifest(changed)
        with self.assertRaises(wb.BundleError):
            wb.parse_manifest(b'{"schema":1,"schema":1}')
        with self.assertRaises(wb.BundleError):
            wb.parse_manifest(b" " * (wb.MAX_MANIFEST_BYTES + 1))

    def test_compatibility_and_corruption_fail_without_fallback(self):
        self.write()
        manifest = self.export()
        for signature, sources in (("w1s0", {}), ("w0s0", {"release": "different"})):
            with self.assertRaises(wb.BundleError):
                wb.PinnedPlanCache(self.bundle, signature, sources)
        with mock.patch.object(wb, "generator_identity", return_value={"code": "other", "native": 116}):
            with self.assertRaises(wb.BundleError):
                wb.PinnedPlanCache(self.bundle, "w0s0", {})
        pc = wb.PinnedPlanCache(self.bundle, "w0s0", {})
        path = self.bundle / "blobs" / (manifest["artifacts"][0]["sha256"] + ".npz")
        path.write_bytes(b"bad")
        with self.assertRaises(wb.BundleError):
            pc.claim(KEY)
        path.unlink()
        with self.assertRaises(wb.BundleError):
            pc.claim(KEY)

    def test_malformed_npz_arrays_rejected(self):
        baseline = self.arrays()
        for field, value in (("p", np.array([[0., np.nan], [1., 2.], [3., 4.]])),
                             ("z", np.array([1., np.inf, 3.])), ("p_n", np.array([-1])),
                             ("z_n", np.array([2])), ("b_n", np.array([2])),
                             ("p", np.array([{"pickle": True}], dtype=object)),
                             ("c", np.full((2, 6), np.inf)), ("p", np.ones((3, 3)))):
            with self.subTest(field=field), self.assertRaises(wb.BundleError):
                wb.validate_artifact(self.encoded({**baseline, field: value}))
        meta = json.loads(baseline["meta"].tobytes())
        meta["plans"] = []
        with self.assertRaises(wb.BundleError):
            wb.validate_artifact(self.encoded({**baseline, "meta": np.frombuffer(json.dumps(meta).encode(), np.uint8)}))

    def test_zip_member_and_allocation_header_rejected(self):
        baseline = self.arrays()
        raw = self.encoded(baseline)
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(out, "w") as dest:
            for name in source.namelist():
                dest.writestr("../meta.npy" if name == "meta.npy" else name, source.read(name))
        with self.assertRaises(wb.BundleError):
            wb.validate_artifact(out.getvalue())
        out = io.BytesIO()
        header = io.BytesIO()
        np.lib.format.write_array_header_1_0(header, {"descr": "<f8", "fortran_order": False, "shape": (10 ** 10, 2)})
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(out, "w") as dest:
            for name in source.namelist():
                dest.writestr(name, header.getvalue() if name == "p.npy" else source.read(name))
        with self.assertRaises(wb.BundleError):
            wb.validate_artifact(out.getvalue())

    def test_non_string_feature_ids_rejected_before_export(self):
        # Graders test tunnel suffixes and sort feature IDs, so even a lone
        # integer can fail after loading; mixed string/integer IDs also cannot sort.
        for ids in ((7,), ("road", 7), (True,), (None,), ("",)):
            with self.subTest(ids=ids):
                self.assertTrue(self.pc.store(KEY, [(fid, self.plan()) for fid in ids],
                                              [], [], [], persist=True))
                data = Path(self.pc._path(KEY)).read_bytes()
                with self.assertRaises(wb.BundleError):
                    wb.validate_artifact(data)
                with self.assertRaises(wb.BundleError):
                    self.export()
                self.assertFalse(self.bundle.exists())

    def test_two_anchors_consume_same_prepared_grades_without_map_fetch(self):
        f = features([[(-300, 20), (300, 20)], [(0, -300), (0, 300)]], ["residential", "secondary"])
        a = grading.Grader(FRAME, FakeMD(f), lambda la, lo: 10. + 0 * np.asarray(la))
        a.shared = plancache.for_grader(str(self.root), a, None, None)
        lat, lon, _ = FRAME.enu_to_geodetic(np.linspace(-200, 200, 50), np.linspace(-30, 60, 50), 0.)
        expected = a.apply(lat, lon, np.zeros(50))
        self.pc = a.shared
        self.export()
        os.environ[wb.ENV] = str(self.bundle)
        os.environ.pop(plancache.RUN_ENV)  # pin cannot silently disappear outside an ordinary run
        for frame in (FRAME, geodesy.EnuFrame(LAT, LON + .01)):
            md = FakeMD(f)
            md.cell = mock.Mock(side_effect=AssertionError("No map requests allowed"))
            b = grading.Grader(frame, md, lambda la, lo: (_ for _ in ()).throw(AssertionError("No DEM build allowed")))
            b.shared = plancache.for_grader(str(self.root / "isolated-client"), b, None, None)
            np.testing.assert_array_equal(b.apply(lat, lon, np.zeros(50)), expected)
            md.cell.assert_not_called()
            self.assertGreater(b.shared.stats["read"], 0)

    def test_rejected_setup_does_not_publish_unpinned_graders(self):
        import earthd
        import blockworker
        dem = SimpleNamespace(sample=lambda la, lo, zoom: np.zeros(np.shape(la)))
        server = SimpleNamespace(grading=True, grader_lock=threading.Lock(), mapdata=FakeMD(None),
                                 dem=dem, road_widths=None, cache_dir=str(self.root), sea=None)
        anchor = SimpleNamespace(grader=None, frame=FRAME)
        worker = blockworker._State.__new__(blockworker._State)
        worker.__dict__.update(lock=threading.Lock(), chunks={}, graders={}, grading=True,
                               md=FakeMD(None), dem=dem, widths=None, cache_dir=str(self.root))
        with mock.patch.object(plancache, "for_grader", side_effect=wb.BundleError("mismatch")):
            for _ in range(2):
                with self.assertRaises(wb.BundleError):
                    earthd.Server.grader_for(server, anchor)
                with self.assertRaises(wb.BundleError):
                    worker.anchor(1, LAT, LON)
                self.assertIsNone(anchor.grader)
                self.assertNotIn(1, worker.graders)
                self.assertNotIn(1, worker.chunks)

    def test_pinning_disables_revisit_and_ungraded_paths(self):
        import earthd
        import blockworker
        import revisit
        self.assertTrue(revisit.store(str(self.root), "plans", "abc", {"x": b"x"}))
        os.environ[wb.ENV] = str(self.bundle)
        self.assertIsNone(revisit.load(str(self.root), "plans", "abc"))
        self.assertFalse(revisit.store(str(self.root), "plans", "new", {"x": b"x"}))
        with self.assertRaises(wb.BundleError):
            earthd.Server.make_provisional(None, None, 0, 0, None)
        with self.assertRaises(wb.BundleError):
            earthd.Server.grader_for(SimpleNamespace(grading=False), None)
        with self.assertRaises(wb.BundleError):
            blockworker._State(str(self.root), True, False, False, False, False)
        with self.assertRaises(wb.BundleError):
            asyncio.run(earthd.main_async(SimpleNamespace(roads="off", grading="on")))
        with self.assertRaises(wb.BundleError):
            asyncio.run(earthd.main_async(SimpleNamespace(roads="on", grading="on", peers="localhost:47620")))
        with self.assertRaises(wb.BundleError):
            earthd.Server._make_heightmap_here_(None, None, 0, 0, None, 0, 1, provisional=True)


if __name__ == "__main__":
    unittest.main()
