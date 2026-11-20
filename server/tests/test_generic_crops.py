"""Generic crop eligibility preserves existing planted fields and surface exclusions."""
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blockworker  # noqa: E402
import earthd  # noqa: E402
import mapgeom  # noqa: E402
import revisit  # noqa: E402
import surface as sf  # noqa: E402


def grids(n=16, start=0):
    r, c = np.mgrid[:n, start:start + n]
    return 41.1 + r * 0.00001, -104.9 + c * 0.00001


def ring(c0, r0, c1, r1):
    return np.array([[-104.9 + c * 0.00001, 41.1 + r * 0.00001]
                     for c, r in ((c0, r0), (c1, r0), (c1, r1), (c0, r1), (c0, r0))])


def fields(classes, polygons, layer="land_use", kind=None):
    rings = [r for polygon in polygons for r in polygon]
    return mapgeom.Features(
        layer, np.array([str(i) for i in range(len(classes))]),
        np.array(kind or [3] * len(classes), np.uint8),
        np.arange(len(classes) + 1, dtype=np.int64),
        np.cumsum([0] + [len(p) for p in polygons]).astype(np.int64),
        np.cumsum([0] + [len(r) for r in rings]).astype(np.int64),
        np.concatenate(rings), attrs={"class": np.array(classes)})


class GenericCropEligibilityTest(unittest.TestCase):
    def setUp(self):
        self.lat, self.lon = grids()
        self.crop = np.full(self.lat.shape, sf.CROP, np.uint8)
        self.planted = fields(["orchard", "vineyard"],
                              [[ring(1.25, 1.25, 6.25, 6.25)], [ring(8.25, 8.25, 13.25, 13.25)]])

    def apply(self, idx=None, features=None):
        return sf.classify_generic_crops(self.crop if idx is None else idx, self.lat, self.lon,
                                        [self.planted] if features is None else features)

    def test_worldcover_cropland_keeps_special_fields_clear(self):
        idx = sf.classify(np.full(self.lat.shape, 40, np.uint8), np.zeros(self.lat.shape), 1)
        self.assertTrue((idx == sf.CROP).all())
        out = self.apply(idx)
        self.assertEqual(out[3, 3], sf.CROP, "orchard trees retain their field")
        self.assertEqual(out[10, 10], sf.CROP, "vine rows retain their field")
        self.assertEqual(out[7, 7], sf.GENERIC_CROP, "ordinary cropland still grows generic plants")
        self.assertTrue((idx == sf.CROP).all(), "the input surface remains unchanged")

    def test_special_fields_win_over_broad_mapped_farmland_in_either_order(self):
        farmland = fields(["farmland"], [[ring(-1, -1, 17, 17)]])
        for feats in ([farmland, self.planted], [self.planted, farmland]):
            idx = sf.paint_fields(np.full(self.lat.shape, sf.DIRT, np.uint8), self.lat, self.lon, feats)
            out = self.apply(idx, feats)
            self.assertEqual(out[3, 3], sf.CROP)
            self.assertEqual(out[10, 10], sf.CROP)
            self.assertEqual(out[7, 7], sf.GENERIC_CROP)

    def test_only_crop_samples_change_inside_special_fields(self):
        idx = self.crop.copy()
        values = [sf.GRASS, sf.FOREST, sf.DRYGRASS, sf.LAWN, sf.ASPHALT, sf.COBBLE,
                  sf.MUD, sf.ROCK, sf.SNOW, 255]
        for position, material in zip(np.ndindex(4, 4), values):
            idx[2 + position[0], 2 + position[1]] = material
        before = idx.copy()
        out = self.apply(idx)
        np.testing.assert_array_equal(out[before != sf.CROP], before[before != sf.CROP])
        np.testing.assert_array_equal(idx, before)
        self.assertEqual(sf.MATERIALS[sf.CROP][1], sf.MATERIALS[sf.GENERIC_CROP][1])

    def test_mapped_farmland_does_not_grow_plants_on_rock_snow_water_or_holes(self):
        land = np.full(self.crop.shape, sf.GRASS, np.uint8)
        for row, material in enumerate((sf.ROCK, sf.SNOW, sf.MUD, 255)):
            land[row, :] = material
        before = land.copy()
        farmland = fields(["farmland"], [[ring(-1, -1, 17, 17)]])
        painted = sf.paint_fields(land, self.lat, self.lon, [farmland])
        self.assertTrue((painted == sf.CROP).all())
        out = sf.classify_generic_crops(painted, self.lat, self.lon, [farmland], land=land)
        self.assertTrue((out[:4] == sf.CROP).all(), "keep existing soil without generic plants on unsuitable land")
        self.assertTrue((out[4:] == sf.GENERIC_CROP).all())
        np.testing.assert_array_equal(land, before)

    def test_holes_remain_eligible_and_overlapping_fields_combine_by_union(self):
        orchard = fields(["orchard"], [[ring(1.25, 1.25, 13.25, 13.25), ring(4.25, 4.25, 10.25, 10.25)]])
        vineyard = fields(["vineyard"], [[ring(5.25, 5.25, 7.25, 7.25)]])
        without_overlap = self.apply(features=[orchard])
        self.assertEqual(without_overlap[6, 6], sf.GENERIC_CROP, "an interior ring is not part of the planted field")
        for features in ([orchard, vineyard], [vineyard, orchard]):
            out = self.apply(features=features)
            self.assertEqual(out[6, 6], sf.CROP, "a second field within that hole remains protected")
            self.assertEqual(out[9, 9], sf.GENERIC_CROP)
            self.assertEqual(out[3, 3], sf.CROP)

    def test_neighboring_blocks_agree_on_their_overlap(self):
        out = self.apply()
        lat, lon = grids(start=4)
        other = sf.classify_generic_crops(self.crop, lat, lon, [self.planted])
        np.testing.assert_array_equal(out[:, 4:], other[:, :12])

    def test_ordinary_farmland_nonpolygons_and_other_layers_are_unchanged(self):
        for feature in (fields(["farmland"], [[ring(-1, -1, 17, 17)]]),
                        fields(["vineyard"], [[ring(-1, -1, 17, 17)]], kind=[2]),
                        fields(["orchard"], [[ring(-1, -1, 17, 17)]], layer="infrastructure")):
            self.assertTrue((self.apply(features=[feature]) == sf.GENERIC_CROP).all())

    def test_empty_features_and_noncrop_blocks_do_no_polygon_work(self):
        self.assertTrue((self.apply(features=[]) == sf.GENERIC_CROP).all())
        grass = np.full(self.crop.shape, sf.GRASS, np.uint8)
        with mock.patch.object(sf, "grid_affine", side_effect=AssertionError("no crop work needed")):
            self.assertIs(self.apply(grass), grass)

    def test_crop_masks_and_terrain_share_the_same_protected_footprint(self):
        out = self.apply()
        with tempfile.TemporaryDirectory() as directory:
            masks = sf.write_masks(out[::-1], directory, "crops")
            mask = np.asarray(Image.open(Path(directory) / masks[sf.GENERIC_CROP]))
            np.testing.assert_array_equal(mask, np.where(out[::-1] == sf.GENERIC_CROP, 255, 0))
            name, used = sf.write_ter(directory, "crops.ter", np.zeros(out.shape, np.uint16), out)
            data = (Path(directory) / name).read_bytes()
            n = struct.unpack_from("<I", data, 1)[0]
            local = np.frombuffer(data, np.uint8, n * n, 5 + 2 * n * n).reshape(n, n)
            lookup = np.full(256, 255, np.uint8)
            lookup[:len(used)] = used
            actual = lookup[local]
            np.testing.assert_array_equal(actual[1:-1, 1:-1], out[1:-1, 1:-1])

    def test_new_carrier_keeps_existing_persisted_material_indices(self):
        self.assertEqual(sf.DIRT, 15)
        self.assertEqual(sf.CROP, 18)
        self.assertEqual(sf.SAND, 19)
        self.assertEqual(sf.GENERIC_CROP, 20)
        self.assertEqual(sf.MATERIALS[sf.CROP], ("bngEarthGroundCrop", "DIRT"))
        self.assertEqual(sf.MATERIALS[sf.GENERIC_CROP], ("bngEarthGroundGenericCrop", "DIRT"))


class CropGenerationPathsTest(unittest.TestCase):
    """Run both real material passes, including a fold that restores crop soil."""
    def test_worker_and_server_guard_the_final_folded_samples(self):
        lat, lon = grids()
        planted = fields(["orchard"], [[ring(1.25, 1.25, 10.25, 10.25)]])
        surface = SimpleNamespace(block_classes=lambda *a: (np.full(lat.shape, 40, np.uint8), True),
                                  roads=lambda *a: ([], True), lots=lambda *a: [planted])
        state = SimpleNamespace(surface=surface, sea=None, airports=None, md=None, mapdata=None,
                                anchor=lambda *a: (None, None), out_dir="unused",
                                hm_name=lambda key: "crop.png", layout=SimpleNamespace(block=16))
        anchor = SimpleNamespace(id=1, frame=object(), hm_zoom=16, chunks=None)
        captures = []

        def generate(*args, **kwargs):
            kwargs["keep"].update(lat=lat, lon=lon, u=np.zeros(lat.shape))
            return {}

        class Written(Exception):
            pass

        def capture(idx, *args):
            captures.append(idx[::-1].copy())
            raise Written()

        with mock.patch.object(blockworker, "_s", state), \
                mock.patch.object(blockworker, "_take_pads"), mock.patch.object(blockworker, "_pin_zoom"), \
                mock.patch.object(blockworker, "plan_ahead"), mock.patch.object(blockworker, "sea_ahead"), \
                mock.patch.object(blockworker.l0roads, "post_for"), \
                mock.patch.object(blockworker.heightmap, "generate", side_effect=generate), \
                mock.patch.object(sf, "fold_layers", side_effect=lambda *a: np.full(lat.shape, sf.CROP, np.uint8)), \
                mock.patch.object(sf, "write_masks", side_effect=capture):
            with self.assertRaises(Written):
                blockworker._build(1, 41.1, -104.9, 16, 1 / 64, 0, 0, 16, "unused", "crop.png", provisional=True)
            with self.assertRaises(Written):
                earthd.Server._make_heightmap_here_(state, anchor, 0, 0, None, time.perf_counter(), 1 / 64,
                                                   provisional=True)
        self.assertEqual(len(captures), 2)
        for idx in captures:
            self.assertEqual(idx[4, 4], sf.CROP, "the guard runs after folding in each generator")
            self.assertEqual(idx[12, 12], sf.GENERIC_CROP)


if __name__ == "__main__":
    unittest.main()
