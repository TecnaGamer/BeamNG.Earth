"""The build machine's readers of Germany's surface models (tools/de_dom_tiles.py, tools/build_de_decks.py,
dedom's WCS answers): tile names, the index, zip members, XYZ grids, thinned 0.2 m DOMs, ranged TIFF blocks,
multipart WCS answers and the WCS subset axes."""
import io
import os
import sys
import tempfile
import unittest
import zipfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
try:
    import tifffile
except ImportError:         # (the build machine's library; the game does not need it)
    tifffile = None
import dedom  # noqa: E402
import de_dom_tiles as t  # noqa: E402


def tiff(a, x0, y0, res=1.0, tile=None):
    buf = io.BytesIO()
    tifffile.imwrite(buf, np.asarray(a, np.float32), tile=tile, compression="zlib" if tile else None,
                     extratags=[(33550, "d", 3, (res, res, 0.0)), (33922, "d", 6, (0.0, 0.0, 0.0, x0, y0, 0.0))])
    return buf.getvalue()


class NameTest(unittest.TestCase):
    def test_tile_names(self):
        self.assertEqual(t.tile_of_name("dom1_32_445_5418_2_bw.zip"), (445, 5418, 2))
        self.assertEqual(t.tile_of_name("dom1_32465_5896_1_hb.xyz"), (465, 5896, 1))
        self.assertEqual(t.tile_of_name("dgm1_33360_5598_2_sn_tiff.zip"), (360, 5598, 2))
        self.assertEqual(t.tile_of_name("bdom20nc_32_425_6002_1_sh_2024.tif"), (425, 6002, 1))
        self.assertIsNone(t.tile_of_name("readme.txt"))

    def test_index_newest_wins_and_covers_its_km(self):
        ix = t.Index()
        ix.add(500, 5900, 2, "old", rank=2020)
        ix.add(500, 5900, 1, "new", rank=2025)
        self.assertEqual(ix.at(500, 5900)[0], "new")
        self.assertEqual(ix.at(501, 5901)[0], "old")
        self.assertIsNone(ix.at(502, 5900))


@unittest.skipIf(tifffile is None, "tifffile")
class GridTest(unittest.TestCase):
    def test_xyz_with_header(self):
        body = b"x y z\n" + b"\n".join(f"{x + 0.5} {y + 0.5} {x * 10 + y}".encode()
                                        for x in (1000, 1001) for y in (2000, 2001))
        g = t.grid_of_xyz(body)
        got = g.at(np.array([1001.5, 1000.2]), np.array([2000.5, 2001.9]))
        self.assertEqual(got.tolist(), [10010 + 2000, 10000 + 2001])

    def test_a_02m_dom_thinned_to_1m(self):
        a = np.arange(100, dtype=np.float32).reshape(10, 10)
        g = t.grid_of_tiff(io.BytesIO(tiff(a, 500.0, 6000.0, res=0.2)))
        self.assertEqual(g.a.shape, (2, 2))
        self.assertEqual(g.dx, 1.0)
        self.assertEqual(float(g.at(np.array([500.5]), np.array([5999.5]))[0]), float(a[2, 2]))

    def test_ranged_blocks_read_only_what_is_asked(self):
        a = np.random.default_rng(1).random((512, 512)).astype(np.float32) * 100
        body = tiff(a, 700000.0, 5300000.0, tile=(256, 256))
        asked = []

        def fetch_range(url, lo, hi):
            asked.append((lo, hi))
            return 206, body[lo:hi + 1], len(body)
        g = t.grid_of_tiff(t.RangeFile(fetch_range, "u"), blockwise=True)
        got = g.at(np.array([700000.5, 700300.5]), np.array([5299999.5, 5299700.5]))
        self.assertAlmostEqual(float(got[0]), float(a[0, 0]), places=4)
        self.assertAlmostEqual(float(got[1]), float(a[299, 300]), places=4)
        self.assertLess(sum(h - lo + 1 for lo, h in asked), len(body))

    def test_a_server_without_ranges_gives_the_whole_file(self):
        body = tiff(np.full((4, 4), 7.0), 0.0, 4.0)
        f = t.RangeFile(lambda u, lo, hi: (200, body, None), "u")
        self.assertEqual(f.read(), body)

    def test_zip_member_of_the_km(self):
        s = t.Sampler.__new__(t.Sampler)
        names = ["d/dom1_32_513_5402_1_bw_2023.tif", "d/dom1_32_513_5403_1_bw_2023.tif", "d/x.pdf"]
        self.assertEqual(s._member(names, None, 513, 5403), names[1])
        self.assertEqual(s._member(["Gitternetz/dom1_32465_5896_1_hb.xyz"], "dom1_32465_5896_1_hb.xyz", 465, 5896),
                         "Gitternetz/dom1_32465_5896_1_hb.xyz")

    def test_a_maintenance_page_is_no_tile(self):
        s = t.Sampler.__new__(t.Sampler)
        s.zips, s.fetch = {}, (lambda u: b"<html>Wartung</html>")
        with self.assertRaises(t.ServiceDown):
            s._zip("https://x/dom.zip", ranged=False)


@unittest.skipIf(tifffile is None, "tifffile")
class WcsTest(unittest.TestCase):
    def test_multipart_answer(self):
        body = tiff(np.full((3, 3), 42.0), 600000.0, 5800000.0)
        mp = b"--wcs\r\nContent-Type: application/gml+xml\r\n\r\n<gml/>\r\n--wcs\r\nContent-Type: image/tiff\r\n\r\n" + \
            body + b"\r\n--wcs--\r\n"
        a = dedom.read_tiff(mp)
        self.assertEqual(float(a[0][1, 1]), 42.0)

    def test_subset_axes(self):
        self.assertIn("SUBSET=E(1,2)&SUBSET=N(3,4)", dedom.url("b", "c", 1, 3, 2, 4, ("E", "N")))
        he = next(s for s in dedom.SERVICES if s["name"] == "he")
        self.assertEqual(he["axes"], ("E", "N"))

    def test_every_state_but_saarland(self):
        self.assertEqual(len({s["name"] for s in dedom.SERVICES}), 15)
        self.assertNotIn("sl", {s["name"] for s in dedom.SERVICES})


class TlsTest(unittest.TestCase):
    """The build's TLS context (build_de_decks.tls_context): Schleswig-Holstein's intermediate from the file shipped in
    tools/, checked against its pinned SHA-256, added for chain building only (no partial chains)."""

    def test_pinned_intermediate_loaded(self):
        import ssl
        import build_de_decks
        ctx = build_de_decks.tls_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertFalse(ctx.verify_flags & getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0))
        names = [dict(x[0] for x in c["subject"]).get("commonName") for c in ctx.get_ca_certs()]
        self.assertIn("D-TRUST BR CA 1-20-1 2020", names)

    def test_wrong_certificate_refused(self):
        import ssl
        import build_de_decks
        with open(build_de_decks.SH_INTERMEDIATE, encoding="ascii") as fh:
            der = bytearray(ssl.PEM_cert_to_DER_cert(fh.read()))
        der[-40] ^= 0x01            # (another certificate: one bit of its signature)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "other.pem")
            with open(p, "w", encoding="ascii") as fh:
                fh.write(ssl.DER_cert_to_PEM_cert(bytes(der)))
            with self.assertRaises(ValueError):
                build_de_decks.tls_context(p)
            with open(p, "w", encoding="ascii") as fh:
                fh.write(ssl.DER_cert_to_PEM_cert(b"x" * 64))     # (not a certificate at all)
            with self.assertRaises(ValueError):
                build_de_decks.tls_context(p)


if __name__ == "__main__":
    unittest.main()
