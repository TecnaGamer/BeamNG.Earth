"""Optional LuaJIT 2.1 checks (pip install --target PATH lupa==2.6).

The test runtime is development-only, not a game/server dependency.
"""
import argparse
from pathlib import Path
import sys
import json

parser = argparse.ArgumentParser()
parser.add_argument('--runtime', required=True)
parser.add_argument('tests', nargs='+')
args = parser.parse_args()
sys.path.insert(0, str(Path(args.runtime).resolve()))
from lupa.luajit21 import LuaRuntime

root = Path(__file__).resolve().parents[1]
for name in args.tests:
    test = (root / 'mod' / 'tests' / name).resolve()
    if test.parent != root / 'mod' / 'tests' or not test.name.startswith('test_') or test.suffix != '.lua':
        raise ValueError('Invalid test name')
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute("os.exit=function(n) if n~=0 then error('Test failed: '..tostring(n)) end end")
    lua.globals().arg = lua.table_from({0: test.as_posix()})
    if test.name == 'test_nl_testset.lua':
        fixture = json.loads((root / 'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/nl_testset/manifest.json').read_text(encoding='utf8'))
        fixture['signs'] = {key: value for key, value in fixture['signs'].items() if not value.get('catalog_source')}
        def to_lua(value):
            if isinstance(value, dict):
                return lua.table_from({key: to_lua(item) for key, item in value.items()})
            if isinstance(value, list):
                return lua.table_from([to_lua(item) for item in value])
            return value
        lua.globals().nlTestFixture = to_lua(fixture)
        lua.globals().arg[1] = '__nl_test_fixture__'
        lua.execute("local original=dofile; dofile=function(path) if path=='__nl_test_fixture__' then return nlTestFixture end; return original(path) end")
    lua.execute(test.read_text(encoding='utf-8'), name=test.as_posix())
