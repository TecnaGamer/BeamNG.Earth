"""Real loopback TCP -> protocol framing -> LuaJIT client, using the country ZIPs.

Needs the server's normal dependencies and optional lupa==2.6 in --lua-runtime.
Never connects to a running game/server and never extracts tiles.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'server'))
import nlsigns
import protocol
import earthd

PLACES = [('Blerick', 51.36711, 6.14178), ('Amsterdam', 52.3676, 4.9041),
          ('Rotterdam', 51.9244, 4.4777), ('Den Haag', 52.0705, 4.3007),
          ('Utrecht', 52.0907, 5.1214), ('Groningen', 53.2194, 6.5665),
          ('Maastricht', 50.8514, 5.6910), ('Enschede', 52.2215, 6.8937),
          ('Leeuwarden', 53.2012, 5.7999), ('Vlissingen', 51.4426, 3.5736),
          ('Den Burg (Texel)', 53.0545, 4.7976)]


def check_lua(frame, query, revision, expected):
    from lupa.luajit21 import LuaRuntime
    lua = LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    g.module_root = (root / 'mod/beamng_earth/lua/ge/extensions').as_posix()
    def table(value):
        if isinstance(value, dict):return lua.table_from({k: table(v) for k, v in value.items()})
        if isinstance(value, list):return lua.table_from([table(v) for v in value])
        return value
    g.jsonDecode = lambda text: table(json.loads(text))
    g.query = table(query);g.revision = revision;g.wire = frame
    lua.execute('''
      package.path=module_root..'/?.lua;'..package.path
      package.loaded['beamngEarth/net']={send=function(q)sent=q;return true end}
      extensions={load=function()end,beamngEarth_nlndw={startRemote=function(p)provider=p end,stop=function()end}}
      remote=require('beamngEarth/nlremote')
      remote.session({nlSigns=1,nlSignsRevision=revision});remote.update(1,{active=true,anchorId=1})
      provider.neighborhood(query.x,query.y,query.keys)
      assert(sent.id==query.id and #sent.keys==#query.keys)
      local rx=require('beamngEarth/framing').newReassembler()
      rx:feed(wire:sub(1,7));assert(rx:pop()==nil)
      rx:feed(wire:sub(8));local payload=assert(rx:pop())
      assert(remote.onMessage(jsonDecode(payload)))
      assert(remote.report().cached_tiles==9)
    ''')
    count = 0
    for key, records in expected.items():
        rows = g.provider.get(key)['records']
        assert len(rows) == len(records)
        for i, row in enumerate(records, 1):
            actual = rows[i]
            for field, value in zip(('id', 'lat', 'lon', 'asset', 'bearing_deg'), row[:5]):
                assert actual[field] == value, (key, field)
            assert [actual['supplementary_assets'][j] for j in range(1, len(actual['supplementary_assets'])+1)] == row[5]
            count += 1
    return count


async def probe(args):
    store = nlsigns.Store(args.store)
    from types import SimpleNamespace
    async def connected(reader, writer):
        conn = object.__new__(earthd.Connection)
        conn.s = SimpleNamespace(nl_signs=store)
        conn.reader, conn.writer, conn.tasks = reader, writer, set()
        conn.nl_signs_busy, conn.nl_signs_last_request = False, -float('inf')
        try:
            while True:
                msg = await protocol.read_message(reader)
                if msg is None:break
                conn.dispatch(msg)
                await writer.drain()
        finally:
            await asyncio.gather(*tuple(conn.tasks), return_exceptions=True)
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(connected, '127.0.0.1', 0)
    results = []
    try:
        async with server:
            port = server.sockets[0].getsockname()[1]
            for name, lat, lon in PLACES:
                x, y = nlsigns.tile_of(lat, lon)
                keys = [f'g17_{a}_{b}' for a in range(x-1, x+2) for b in range(y-1, y+2)]
                query = dict(type='nl_sign_tiles', id=1, x=x, y=y, keys=keys)
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                start = time.perf_counter()
                writer.write(protocol.encode(query));await writer.drain()
                response = await asyncio.wait_for(protocol.read_message(reader), 15)
                duration = (time.perf_counter() - start) * 1000
                writer.close();await writer.wait_closed()
                assert response and not response.get('error'), response
                expected = {k: store._tile(k) for k in keys}  # worker finished; read-only cached comparison
                records = check_lua(protocol.encode(response), query, store.revision, expected)
                assert records > 0, name
                old_bytes = sum(store.owners[k].getinfo(nlsigns.PREFIX+'render/'+k+'.json').file_size
                                for k in keys if k in store.owners)
                results.append(dict(place=name, records=records, wire_bytes=len(protocol.encode(response)),
                                    v9_tile_bytes=old_bytes, loopback_request_ms=round(duration, 3)))
    finally:
        server.close();await server.wait_closed();store.close()
    total_wire = sum(r['wire_bytes'] for r in results)
    total_v9 = sum(r['v9_tile_bytes'] for r in results)
    report = dict(status='PASS', transport='existing companion TCP protocol; real Connection.dispatch',
                  client='actual nlremote and framing in LuaJIT 2.1; game renderer not exercised',
                  extracted_tiles=0, places=results, wire_bytes=total_wire, v9_tile_bytes=total_v9,
                  reduction_percent=round((1-total_wire/total_v9)*100, 2))
    Path(args.output).write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--store', required=True)
    parser.add_argument('--lua-runtime', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.lua_runtime).resolve()))
    asyncio.run(probe(args))
