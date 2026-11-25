#!/usr/bin/env python3
"""Replay far imagery outages against a server tree, using synthetic tiles and an in-memory HTTP transport.

    python3 tools/portcheck/farfail.py --server-dir BEFORE/server --out before.json
    python3 tools/portcheck/farfail.py --server-dir AFTER/server --out after.json --compare before.json

Each run starts with empty caches. The manifest records the shape and DDS digests, source texel counts,
credits and tile state at publication, including a failed retexture and recovery on the same material.
It also records each far worker's texture result under its strict source policy.
"""
import argparse
from concurrent.futures import Future
from contextlib import ExitStack
import math
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pcommon


def tile_state(server, key, info, used, messages):
    out = Path(server.out_dir)
    shape = out / server.tile_name(*key)
    dds = out / info.texture if info.texture else None
    return {
        "shape": pcommon.sha(shape.read_bytes()), "material": info.material, "texture": info.texture,
        "dds": pcommon.sha(dds.read_bytes()) if dds and dds.is_file() else None,
        "tex_bytes": info.tex_bytes, "revision": info.rev, "stale": info.stale, "degraded": key in server.degraded,
        "repeat": info.retex_repeat, "credits": sorted(info.credits), "used": used, "messages": messages.copy(),
        "offline": server.net.offline, "offline_flag": server.net.offline_flag,
    }


def worker_texture(server, anchor, tile, path):
    import blockworker
    import farfield
    im = server.imagery
    tex = dict(path=str(path), px=server.tile_tex_px(anchor, tile), chain=[src.name for src in im.chain],
               tone=server.tone, leaf_on=im.leaf_on, season_mode=im.season_mode)
    if isinstance(tile, farfield.GTile):
        zoom = farfield.global_dem_zoom(tile.z, server.layout.cells)
        ref = farfield.global_dem_zoom(tile.z + 1, server.layout.cells)
        _, result = blockworker.far_global_tile(tile.z, tile.x, tile.y, zoom, ref, server.layout, tex)
    else:
        _, result = blockworker.far_tile(anchor.id, anchor.lat, anchor.lon, tile, anchor.zooms[tile.level], None,
                                         anchor.zooms[tile.level - 1], False, server.layout, tex)
    return {"result": result, "dds": pcommon.sha(path.read_bytes()) if path.exists() else None}


def replay(root, scenario):
    import blockworker
    import credits
    import earthd
    import farfield
    import geotiles
    import imagery
    import matslots
    import netfetch
    from test_credits import ServedDem
    from test_imagery import FakeNet, background, png
    from test_server import LAT, LON

    def serve(name, z, x, y):
        if scenario == "no_coverage":
            return 404, b""
        rgb = background(z, x, y)
        rgb[..., 0] = 100 if name == "usgs" else 180
        if name == "usgs" and scenario in ("mixed", "incomplete"):
            rgb[:, 80:176] = 0  # a nodata band for the next source to fill
        return 200, png(rgb)

    class Provider(FakeNet):
        """A public renderer can fail independently of its HTTP inputs and can correct successful RGB."""
        down = True

        def corrected(self, render, *args, **kwargs):
            if self.down:
                raise imagery.ImageryError("provider unavailable")
            rgb, used = render(*args, **kwargs)
            rgb = rgb.copy()
            rgb[..., 1] //= 2
            return rgb, dict(used, colormatch=used["usgs"])

        def render(self, *args, **kwargs):
            return self.corrected(super().render, *args, **kwargs)

        def render_merc(self, *args, **kwargs):
            return self.corrected(super().render_merc, *args, **kwargs)

    with ExitStack() as cleanup:
        now = [1000.0]
        cls = Provider if scenario in ("provider", "provider_offline") else FakeNet
        im = cls(str(root / "imagery"), serve, chain=("usgs", "eox2016"), clock=lambda: now[0])
        cleanup.callback(im.close)
        net = im.fetcher
        server = earthd.Server(str(root), str(root / "cache"), imagery=None, net=net, credits=credits.Credits(),
                               near_visual="mesh", cells=64)
        cleanup.callback(server.tile_pool.shutdown)
        cleanup.callback(server.hm_pool.shutdown)
        server.dem = ServedDem()
        server.imagery = im
        # The material prefix is embedded in the shape bytes. Give both trees the same session salt.
        server.matslots = matslots.MatSlots("0000", held=server.slot_held)
        anchor = server.new_anchor(LAT, LON)
        z = 14
        x, y = geotiles.tile_of(LAT, LON, z)
        quad = geotiles.quad(anchor.frame, x, y, z)
        edge = sum(math.dist(p, quad[(i + 1) % 4]) for i, p in enumerate(quad)) / 4
        tiles = [farfield.Tile(2, 0, 0), farfield.GTile(z, x, y, 3, tuple(quad.ravel()), edge)]

        messages = []

        class Viewer:
            active = anchor.id
            far = earthd.msgs.FarState(earthd._far_cfg())
            send = messages.append
            credits = SimpleNamespace(poke=lambda conn: None)
            slot_holds = {}

        viewer = Viewer()
        server.connections.add(viewer)
        server.vfs_dir = "/temp/beamngEarth/replay"
        viewer.far.set_desired({t.key: t for t in tiles})

        worker = blockworker._State(str(root / "worker"), True, False, False, False, False)
        worker.dem = server.dem
        cleanup.enter_context(patch.object(blockworker, "_s", worker))
        cleanup.enter_context(patch.object(blockworker, "_imagery", return_value=im))

        if scenario == "provider_offline":
            # Cached photos are present, but the provider now rejects the render. Bypassing its
            # public method would silently publish the cached DDS even in explicit offline mode.
            im.down = False
            for tile in tiles:
                server.render_tile(anchor, tile, server.tile_tex_px(anchor, tile), None)
            im.down = True
        net.offline_flag = scenario in ("offline", "provider_offline")
        if scenario == "breaker":
            for host in net.chain_hosts("dem"):
                net.host(host).breaker.trip(60)
        for src in im.chain:
            if scenario == "breaker" or (scenario == "failover" and src.name == "usgs") or (
                    scenario == "incomplete" and src.name == "eox2016"):
                net.host(netfetch.host_of(src.url)).breaker.trip(60)

        # Observe the counts handed to the server, before conversion to the deduplicated credit IDs.
        draw = server.render_tile
        used = None

        def observe(*args, **kwargs):
            nonlocal used
            photo, used = draw(*args, **kwargs)
            return photo, used

        server.render_tile = observe
        records = {}
        stages = ("initial",)
        if scenario in ("breaker", "provider", "incomplete"):
            stages += ("retry", "recovered")
        for stage in stages:
            if stage == "recovered":
                now[0] += 61
                # One successful half-open probe per imagery host before the concurrent mosaic reads.
                for src in im.chain:
                    if net.host(netfetch.host_of(src.url)).breaker.is_open():
                        net.get(src.url.format(z=z, x=x, y=y))
                if scenario == "provider":
                    im.down = False
            for tile in tiles:
                used = None
                messages.clear()
                key = server.far_ck(anchor.id, tile.key)
                info = server.make_tile(anchor, tile, server.tiles.get(key))
                future = Future()
                future.set_result(info)
                server._tile_done(key, future)
                record = tile_state(server, key, info, used, messages)
                with patch.object(im, "strict", True):
                    record["worker"] = worker_texture(server, anchor, tile, root / f"{stage}-{tile.key}.dds")
                records[f"{scenario}/{stage}/{tile.key}"] = record
            viewer.far.set_sent({t.key: t for t in tiles})
        return records


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server-dir", type=Path, default=Path(pcommon.SERVER))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    if args.compare and args.out.resolve() == args.compare.resolve():
        parser.error("--out and --compare must be different files")
    sys.path[:0] = [str(args.server_dir.resolve()), str(args.server_dir.resolve() / "tests")]
    os.environ.update(BNE_REVISIT="off", BNE_GEN_GROUND="off")
    import npyguard
    npyguard.apply()
    manifest = {}
    with tempfile.TemporaryDirectory() as scratch:
        for scenario in ("offline", "breaker", "failover", "mixed", "incomplete", "no_coverage",
                         "provider", "provider_offline"):
            manifest.update(replay(Path(scratch) / scenario, scenario))
    pcommon.write_json(args.out.resolve(), manifest)
    print(f"Recorded {len(manifest)} far tile states in {args.out}")
    if args.compare:
        baseline = pcommon.read_json(args.compare)
        manifest = pcommon.read_json(args.out)  # canonical key order for json_diff
        different = sorted(k for k in baseline.keys() | manifest.keys() if baseline.get(k) != manifest.get(k))
        for key in different:
            for path, before, after in pcommon.json_diff(baseline.get(key), manifest.get(key), key):
                print(f"{path}: {before} -> {after}")
        print(f"{len(different)} different")
        return bool(different)


if __name__ == "__main__":
    sys.exit(main())
