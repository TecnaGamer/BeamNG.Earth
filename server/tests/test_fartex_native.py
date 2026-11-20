"""Pixel identity at the finished imagery cache, RGB resize and complete BC1 file boundaries."""
import hashlib
import io
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geotiles
import imagery
import rockface
import surface


class FarTextureNativeTest(unittest.TestCase):
    def test_full_dds_against_version_556(self):
        # Scalar BC1 makes these reference digests portable to hosts without the ISPC build.
        a = np.random.default_rng(559).integers(0, 256, (32, 64, 3), np.uint8)
        hashes = (
            "65a08f26fdb6b5d0724fedd15b89a89f976b6752a8c52f03731581ee28a0c8a5",
            "b9759815bd16183b1f0925c7137391db85322d2303c0b5843bceaf074a1f14e2",
            "29657de56fd3901c66ebfaaa6f96a15e70e46d6921844dc2fc908e0a208c88e7",
            "2a222ab556219396ff59345629fb1b46b726ce0e465952528b0ec58c34e2f9ca",
        )
        with mock.patch.dict(os.environ, BNE_BC1_REF="1"):
            files = [imagery.encode_dds(a, srgb, fade) for srgb in (False, True) for fade in (None, (a[:, ::-1], .7))]
        self.assertEqual(tuple(hashlib.sha256(f).hexdigest() for f in files), hashes)

    def test_fall_and_steep_rock_dds_against_version_556(self):
        rgb = np.random.default_rng(559).integers(10, 175, (256, 256, 3), np.uint8)
        rgb[..., 1] += 70
        z, x, y, px = 14, 3383, 6123, 64
        where = imagery.MercSquare(z, x, y, px)
        height = np.array([[13., 30., 0.], [1., 21., 48.], [3., 12., 42.]])
        rock = rockface.inputs(geotiles.frame(x, y, z), -16., -16., 32., height, -16., -16., 16.)
        q = np.array([[.375, 255.125], [.375, 255.125]])
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, BNE_BC1_REF="1"):
            reader = imagery.Imagery(d, season_mode="fall")
            self.addCleanup(reader.close)
            canvas = imagery.fast._ef.PhotoCanvas(px, 0)
            canvas.warp([(0, 0, rgb, 255, None)], 1, 1, q, q.T, 1, px)
            canvas.fill(imagery.FALLBACK_RGB, imagery.POLAR_RGB, None)
            path = Path(d) / "fall.dds"
            photo = imagery.FarPhoto(canvas, reader._season_inputs(where))
            size = photo.write(str(path), rock, True)
            self.assertEqual(size, path.stat().st_size)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             "150fcbefe9c2fc8f6fec5995e8848c5a7128da2eef13d45e9088b6e0c88ad028")

    def test_completed_canvas_owns_its_rgb_until_taken(self):
        ef = imagery.fast._ef
        canvas = ef.PhotoCanvas(8, 1)
        with self.assertRaises(ValueError):
            canvas.take_rgb()
        self.assertEqual(canvas.fill((7, 31, 63), (1, 1, 1), None), (36, 0))
        with self.assertRaises(ValueError):
            canvas.holes()
        q = np.zeros((2, 2))
        with self.assertRaises(ValueError):
            canvas.warp([], 1, 1, q, q, 1, 8)
        rgb = canvas.take_rgb()
        np.testing.assert_array_equal(rgb, np.broadcast_to([7, 31, 63], (8, 8, 3)))
        with self.assertRaises(ValueError):
            canvas.take_rgb()

    def test_base_resize_against_pillow(self):
        rng = np.random.default_rng(559)
        for h, w, px in ((1, 7, 13), (19, 1, 3), (181, 63, 91), (16, 8, 4), (32, 32, 64), (33, 33, 7), (7, 7, 19), (19, 17, 1)):
            with self.subTest(shape=(h, w), px=px):
                a = rng.integers(0, 256, (h, w, 3), np.uint8)[:, ::-1]
                ref = Image.fromarray(a).resize((px, px), Image.Resampling.BILINEAR)
                np.testing.assert_array_equal(surface.enlarge(a, px), np.asarray(ref))

    def test_finished_mask_and_raw_stamp(self):
        ef = imagery.fast._ef
        a = np.random.default_rng(559).integers(0, 256, (256, 256, 3), np.uint8)
        mask = np.zeros((256, 256), np.uint8)
        mask[10, 7:9] = 1  # MSB order, across two packed bytes
        qx = np.array([[0., 256.], [0., 256.]])
        with tempfile.TemporaryDirectory() as d:
            path, raw = Path(d) / "photo.bnf", Path(d) / "raw.jpg"
            for uniform in (True, False):
                buf = io.BytesIO()
                Image.fromarray(a).save(buf, "PNG" if uniform else "JPEG")
                body = buf.getvalue()
                stamp = struct.pack("<BBHQQ", 14 | uniform, 1, 0, 7, 123)
                record = b"BNF1" + stamp + struct.pack("<BIII", 2, 17, 23, len(body)) + body + np.packbits(mask).tobytes()
                path.write_bytes(record)
                tile, n = ef.read_finished_tile(path, raw)
                self.assertEqual((n, tile.wait, tile.relit, tile.cm), (len(record), (("cm", (17, 23)),), True, True))
                # Compare a disk-backed masked mosaic with the pre-559 RGB/mask representation of the same tile.
                rgb = a if uniform else ef.jpeg_rgb(body)
                images = []
                for src in (tile.for_mosaic(False), (rgb, 254, mask.astype(bool))):
                    canvas = ef.PhotoCanvas(256, 0)
                    hits, relit, full, _ = canvas.warp([(0, 0, *src)], 1, 1, qx, qx.T, 1, 256)
                    self.assertEqual((hits, relit, full), (65534, 65534, False))
                    none, snow = canvas.fill((12, 34, 56), (0, 0, 0), None)
                    self.assertEqual((none, snow), (2, 0))
                    images.append(canvas.take_rgb())
                np.testing.assert_array_equal(*images)
                path.write_bytes(record[:-1])
                self.assertIsNone(ef.read_finished_tile(path, raw))
            path.write_bytes(record)
            raw.write_bytes(b"changed")
            self.assertIsNone(ef.read_finished_tile(path, raw))
