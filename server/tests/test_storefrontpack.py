"""Exercise folder swaps through the normal road tile entry point."""
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import buildings
import geodesy
import storefrontpack
import threedmr
from test_streamedstores import tile


class StorefrontPackTest(unittest.TestCase):
    def test_live_install_replace_remove(self):
        frame = geodesy.EnuFrame(40., -83.)
        md = tile(frame, "Target", [])
        with (tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, BNE_STOREFRONTS="1"),
              patch("threedmr.region_ids", return_value=[]),
              patch("buildings.road_lines", return_value=[np.array([[0., 15.], [256., 15.]])])):
            root = Path(tmp) / "temp/beamngEarth"
            archive = storefrontpack.archive_path(root)
            archive.parent.mkdir(parents=True)

            def render():
                _, rows = buildings.road_tile_items(frame, 0, 0, md, lambda a, b: np.zeros_like(a), cache_dir=root)
                return rows[0]["local"], threedmr.local_signature(root)

            def install(label, *, invalid_mesh=False):
                from PIL import Image
                sign = io.BytesIO()
                Image.new("RGB", (64, 32), "#778899").save(sign, "PNG")
                stage = archive.with_suffix(".new")
                with zipfile.ZipFile(stage, "w") as z:
                    chain = dict(label=label, style="shopfront", paint="#335555", ink="#ffffff")
                    chain.update(model="sample.glb", width=45, depth=30, sign="sign.png")
                    model = root / "threedmr_cache" / baseline[0]["file"]
                    z.writestr("sample.glb", b"bad asset" if invalid_mesh else model.read_bytes())
                    z.writestr("sign.png", sign.getvalue())
                    sites = [dict(file="sample.glb", lat=1., lon=lon) for lon in (1., 2.)]
                    z.writestr("pack.json", json.dumps(dict(version=1, chains={"Target": chain}, sites=sites)))
                stage.replace(archive)

            baseline = render()
            install("First Market")
            first = render()
            self.assertEqual(first[0]["title"], "First Market")
            self.assertEqual(first[0]["storefront_variant"], "pack")
            self.assertTrue(len(threedmr.load_local_model(root, first[0])[2]))
            placed = threedmr.local_models(root)
            self.assertEqual(len({row["id"] for row in placed}), 2)

            @storefrontpack.tile_scope("root")
            def change_during_build(root):
                before = threedmr.local_signature(root)
                install("Second Market")
                self.assertEqual(render(), first)
                self.assertEqual(threedmr.local_signature(root), before)

            change_during_build(root)
            second = render()
            self.assertEqual(second[0]["title"], "Second Market")
            self.assertNotEqual(first[0]["file"], second[0]["file"])
            self.assertNotEqual(first[1], second[1])
            install("Broken Model", invalid_mesh=True)
            self.assertEqual(render(), second)
            archive.write_bytes(b"incomplete copy")
            self.assertEqual(render(), second)
            archive.unlink()
            self.assertEqual(render(), baseline)

    def test_visible_tiles_refresh_only_when_revision_changes(self):
        import earthd
        from types import SimpleNamespace
        async def exercise():
            jobs = []
            connection = SimpleNamespace(spawn=jobs.append, s=SimpleNamespace(root="unused"), roads=Mock())
            with (patch("storefrontpack.snapshot", side_effect=[None, SimpleNamespace(digest="v1"),
                                                              SimpleNamespace(digest="v1"), None]),
                  patch("earthd.road_tracker", side_effect=lambda conn: Mock()) as tracker):
                for expected in (0, 1, 1, 2):
                    connection._pack_poll_after = 0
                    earthd.Connection._refresh_storefront_pack(connection)
                    await jobs.pop()
                    self.assertEqual(tracker.call_count, expected)
        with patch.dict(os.environ, BNE_STOREFRONTS="1"):
            asyncio.run(exercise())
