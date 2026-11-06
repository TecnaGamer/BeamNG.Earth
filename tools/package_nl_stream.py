"""Small source/client updater; reuses the existing compressed model mod and data.

Patches server/core/navigation; compatible-project is read, never modified.
"""
import argparse
import difflib
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile
import re

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ['server/earthd.py', 'mod/beamng_earth/lua/ge/extensions/beamngEarth/core.lua',
               'server/navgraph.py','mod/beamng_earth/lua/ge/extensions/beamngEarth/navgraph.lua',
               'mod/beamng_earth/lua/ge/extensions/beamngEarth/roads.lua','mod/beamng_earth/lua/ge/extensions/beamngEarth/trees.lua']
PAYLOAD = ['server/nlsigns.py', 'server/tests/test_nlsigns.py',
           'mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_country.lua','mod/tests/test_nl_country.lua',
           'mod/beamng_earth/lua/ge/extensions/beamngEarth/nlremote.lua',
           'mod/beamng_earth/lua/ge/extensions/beamngEarth/nlndw.lua',
           'mod/tests/test_nl_remote.lua', 'mod/tests/test_nl_george_tiles.lua',
           'tools/install_nl_stream.ps1', 'tools/run_lua_jit.py', 'tools/probe_nl_stream.py',
           'tools/package_nl_stream.py', 'docs/NL_SERVER_STREAM_V10.md', 'docs/NL_SIZE_ROTATION_V11.md',
           'tools/build_nl_size_profiles.mjs','mod/tests/test_nl_context.lua',
           'mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_context.lua',
           'mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_placement.lua',
           'mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_testset.lua',
           'mod/tests/test_nl_ndw_stream.lua','mod/tests/test_nl_testset.lua','server/tests/test_navgraph.py',
           'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/nl_testset/size_profiles.json',
           'mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_legacy.lua',
           'mod/beamng_earth/lua/ge/extensions/beamngEarth/nl_replacements.lua','mod/beamng_earth/lua/ge/extensions/beamngEarth/nltestset.lua',
           'mod/tests/test_trees.lua','mod/tests/test_highway_signs.lua','docs/NL_RVV_ONLY_V12.md']


def normalized(text):
    return text.replace('\r\n', '\n')


def compatible_conflicts(text, name):
    """Resolve only the reviewed overlaps with the BGT sign/barrier additions."""
    def resolve(match):
        ours, theirs = match.group(1), match.group(2)
        if name == 'server/earthd.py' and ours.startswith("        elif kind == 'rvv_only':") and theirs.startswith('        elif kind == "direction_signs":'):
            return ours + theirs
        if name.endswith('/roads.lua'):
            if ours.startswith('M.legacySignsSuppressed = false\n') and theirs == "M.SIGN_WHATS = { 'exits', 'mileposts', 'merges', 'speeds', 'dirs' }\n":
                return ours
            if theirs == ours.replace("'railcol', 'exits'", "'railcol', 'barrier', 'exits'") and theirs != ours:
                return theirs
            if theirs == ours.replace("'tunnel', 'rail'", "'tunnel', 'barrier', 'rail'") and theirs != ours:
                return theirs
        raise ValueError('Unreviewed compatible-project conflict: ' + name)
    result = re.sub(r'^<<<<<<<[^\n]*\n(.*?)^=======\n(.*?)^>>>>>>>[^\n]*\n', resolve, text, flags=re.M | re.S)
    if '<<<<<<<' in result:
        raise ValueError('Unresolved compatible-project conflict: ' + name)
    return result


def compatible_navgraph(other, ours):
    """Keep the BGT/tunnel branch's geometry and add only sign-context fields."""
    def replace_once(text, old, new):
        if text.count(old)!=1:
            raise ValueError('Unrecognised compatible navgraph; merge manually')
        return text.replace(old,new,1)
    helper=ours[ours.index('def _set_out_radius('):ours.index('def road_mph(')]
    other=replace_once(other,'def road_mph(',helper+'def road_mph(')
    anchor='                target = min(legal, float(profile[j]), float(profile[j + 1]))\n'
    start=ours.index(anchor)+len(anchor)
    metadata=ours[start:ours.index('                half =',start)]
    other=replace_once(other,anchor,anchor+metadata)
    original='edges.append([a, b, 1 if direction else 0, lo, li, round(target, 2), driv, round(half, 2)])'
    new="edges.append([a, b, 1 if direction else 0, lo, li, round(target, 2), driv, round(half, 2), round(half, 2), {'road_class': spec.cls, 'max_speed_kmh': round(legal * 3.6, 1), 'speed_source': 'mapped_max_speed' if mapped else 'road_class_default'}])"
    return replace_once(other,original,new)


def compatible_navlua(other, ours):
    def replace_once(text,old,new):
        if text.count(old)!=1:raise ValueError('Unrecognised compatible navgraph.lua; merge manually')
        return text.replace(old,new,1)
    helper=ours[ours.index('M.nlRevision=0'):ours.index('M.EDGE_M =')]
    other=replace_once(other,'local M = {}\n','local M = {}\n'+helper)
    other=replace_once(other,'          inRadius = e[8],','          nlRoadContext = e[10],\n          inRadius = e[8],')
    context=ours[ours.index('  local nlContexts={}'):ours.index('  local ts = signalsSystem()',ours.index('  local nlContexts={}'))]
    at=other.index('  local ts = signalsSystem()',other.index('local function apply('))
    other=other[:at]+context+other[at:]
    anchor='  stats.resetMs = (os.clock() - t0) * 1000\n'
    other=replace_once(other,anchor,anchor+'  M.nlContexts=nlContexts;M.nlRevision=M.nlRevision+1\n')
    return replace_once(other,'function M.reset()\n','function M.reset()\n  M.nlContexts={};M.nlRevision=M.nlRevision+1\n')


def build(args):
    out = Path(args.output_directory).resolve()
    out.mkdir(parents=True, exist_ok=True)
    patch = compatible = nav_patch = compatible_nav_patch = ''
    current={};merged_current={}
    for name in INTEGRATION:
        if args.baseline and (Path(args.baseline) / name).exists():
            old = (Path(args.baseline) / name).read_text(encoding='utf8')
        else:
            old = subprocess.check_output(['git', '-c', f'safe.directory={ROOT}', '-C', str(ROOT),
                                           'show', 'HEAD:' + name]).decode('utf8')
        old = normalized(old)
        new = normalized((ROOT / name).read_text(encoding='utf8'))
        current[name]=new
        piece = ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                            fromfile='a/' + name, tofile='b/' + name))
        patch += piece
        if 'navgraph' in name:nav_patch += piece
        if args.compatible_project:
            other = normalized((Path(args.compatible_project) / name).read_text(encoding='utf8'))
            with tempfile.TemporaryDirectory(dir=out) as temp:
                paths = [Path(temp) / label for label in ('ours', 'base', 'theirs')]
                for path, text in zip(paths, (new, old, other)):
                    path.write_text(text, encoding='utf8', newline='\n')
                result = subprocess.run(['git', 'merge-file', '-p', *map(str, paths)], capture_output=True)
                if result.returncode and name=='server/navgraph.py':
                    merged = compatible_navgraph(other,new)
                elif result.returncode and name=='mod/beamng_earth/lua/ge/extensions/beamngEarth/navgraph.lua':
                    merged = compatible_navlua(other,new)
                elif result.returncode:
                    merged = compatible_conflicts(normalized(result.stdout.decode('utf8')), name)
                else:
                    merged = normalized(result.stdout.decode('utf8'))
                if name=='server/navgraph.py' and 'e.append(r1)' in merged:
                    merged=merged.replace('e.append(r1)','_set_out_radius(e, r1)')
            piece = ''.join(difflib.unified_diff(other.splitlines(True), merged.splitlines(True),
                                                      fromfile='a/' + name, tofile='b/' + name))
            compatible += piece
            merged_current[name]=merged
            if 'navgraph' in name:compatible_nav_patch += piece
    patches = {'integration.patch': patch, 'integration-nav.patch': nav_patch}
    if args.compatible_project:
        patches['integration-bgt.patch'] = compatible
        patches['integration-bgt-nav.patch'] = compatible_nav_patch
    for version in (10,11):
        for label, texts in [('base',current),('bgt',merged_current)]:
            baseline=ROOT/'work'/f'v{version}-{label}-baseline'
            if label=='base' and version==11:baseline=ROOT/'work/v11-baseline'
            if not texts or not baseline.exists():continue
            text=''
            for name,new in texts.items():
                old=normalized((baseline/name).read_text(encoding='utf8'))
                text+=''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='a/'+name,tofile='b/'+name))
            patches[f'upgrade-v{version}-{label}.patch']=text
    for name, text in patches.items():
        if len(text) > 100000:
            raise ValueError('Unexpectedly large integration patch; check line endings')
        (out / name).write_text(text, encoding='utf8', newline='\n')
    for name in PAYLOAD:
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    archive = out.parent / ('BeamNG_Earth_NL_v'+str(args.version)+'_update.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted(PAYLOAD + list(patches)):
            z.write(out / name, name)
    print(f'{archive}: {archive.stat().st_size} bytes; no models or country tiles included')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-directory', required=True)
    parser.add_argument('--baseline')
    parser.add_argument('--compatible-project')
    parser.add_argument('--version',type=int,default=12)
    build(parser.parse_args())
