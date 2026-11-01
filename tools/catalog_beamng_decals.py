"""Collect installed BeamNG decal textures into a local, searchable HTML gallery.

Only the extractor is distributed. Output contains proprietary installed game
assets and belongs in the ignored build directory, not in the mod package.

python tools/catalog_beamng_decals.py --content <BeamNG.drive/content>
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import struct
import zipfile

from PIL import Image, ImageDraw

IMAGE_EXTS = {'.dds', '.png', '.jpg', '.jpeg', '.tga', '.bmp'}
ROCK_WORDS = re.compile(r'rock|stone|rubble|gravel|sand|dirt|ground|cliff|pebble|erosion|soil|scree|mud', re.I)


def read_json(blob):
    """BeamNG's older material files allow comments and trailing commas."""
    text = blob.decode('utf-8-sig')
    strings = r'"(?:\\.|[^"\\])*"'
    text = re.sub(strings+r'|//[^\n]*|/\*.*?\*/',
                  lambda m: m[0] if m[0].startswith('"') else '', text, flags=re.S)
    text = re.sub(strings+r'|,(?=\s*[}\]])',
                  lambda m: m[0] if m[0].startswith('"') else '', text)
    return json.loads(text)


def preview_image(blob):
    # Pillow handles BC1_UNORM (71), but not its sRGB-tagged equivalent (72).
    # The block encoding is identical. Decode the unchanged sRGB pixel values;
    # alter only this in-memory preview header, never the extracted original.
    if blob[:4] == b'DDS ' and blob[84:88] == b'DX10' and len(blob) >= 148:
        if struct.unpack_from('<I', blob, 128)[0] == 72:
            blob = blob[:128]+struct.pack('<I', 71)+blob[132:]
    return Image.open(io.BytesIO(blob))


def channel(path, roles):
    """Prefer material bindings over ambiguous legacy filename suffixes."""
    roles = {r.lower() for r in roles}
    name = PurePosixPath(path).name.lower()
    for kind, fields in [('Color', {'basecolormap', 'colormap', 'diffusemap'}),
                         ('Normal', {'normalmap', 'detailnormalmap'}),
                         ('Opacity', {'opacitymap', 'translucencymap'}),
                         ('Roughness', {'roughnessmap', 'detailroughnessmap'}),
                         ('AO', {'ambientocclusionmap'}), ('Metallic', {'metallicmap'}),
                         ('Height', {'heightmap', 'displacementmap'}),
                         ('Detail', {'detailmap', 'detailbasecolormap'}),
                         ('Specular', {'specularmap'})]:
        if roles & fields:
            return kind
    if re.search(r'\.normal\.|_(nm|n|normal)\.', name): return 'Normal'
    if re.search(r'\.color\.|_(d|b|c|diff|diffuse|albedo)\.', name): return 'Color'
    if re.search(r'_ao[_.]', name): return 'AO'
    if re.search(r'_(r|roughness)[_.]', name): return 'Roughness'
    if re.search(r'_(o|opacity|alpha)[_.]', name): return 'Opacity'
    if re.search(r'_(m|metallic)[_.]', name): return 'Metallic'
    if re.search(r'_(h|height|disp)[_.]', name): return 'Height'
    if re.search(r'_(s|specular)[_.]', name): return 'Specular'
    return 'Other'


def records(value):
    if isinstance(value, dict):
        if value.get('class') in ('Material', 'DecalData'):
            yield value
        else:
            for child in value.values(): yield from records(child)
    elif isinstance(value, list):
        for child in value: yield from records(child)


def bindings(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, str) and PurePosixPath(child).suffix.lower() in IMAGE_EXTS:
                yield key, child
            else:
                yield from bindings(child)
    elif isinstance(value, list):
        for child in value: yield from bindings(child)


def collect(content):
    entries, materials, definitions, warnings = {}, [], [], []
    archives = sorted(content.rglob('*.zip'))
    for archive in archives:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                path = info.filename.replace('\\', '/').lstrip('/')
                if PurePosixPath(path).suffix.lower() in IMAGE_EXTS:
                    entries.setdefault(path.lower(), []).append((archive, info.filename, path))
                if path.endswith('.materials.json') or 'manageddecaldata' in path.lower():
                    try:
                        value = read_json(z.read(info))
                    except (ValueError, UnicodeError) as exc:
                        warnings.append({'file': path, 'error': str(exc)})
                        continue
                    for obj in records(value):
                        record = {'source': path, 'archive': str(archive.relative_to(content)), 'object': obj}
                        (definitions if obj['class'] == 'DecalData' else materials).append(record)
    decal_names = {d['object'].get('material') for d in definitions}
    selected = {p for p in entries if 'decal' in p}
    uses, unresolved, selected_materials = {}, [], []

    def resolve(ref, source):
        ref = ref.replace('\\', '/').lstrip('/')
        options = [ref, str(PurePosixPath(source).parent/ref)]
        for option in options:
            for candidate in (option, str(PurePosixPath(option).with_suffix('.dds'))):
                if candidate.lower() in entries:
                    return candidate.lower()
        return None

    for mat in materials:
        obj = mat['object']
        refs = list(bindings(obj))
        name = obj.get('name', obj.get('mapTo', 'unnamed'))
        if not ('decal' in mat['source'].lower() or 'decal' in name.lower()
                or name in decal_names or any('decal' in ref.lower() for _, ref in refs)):
            continue
        selected_materials.append(mat)
        for role, ref in refs:
            target = resolve(ref, mat['source'])
            if target is None:
                unresolved.append({'material': name, 'source': mat['source'], 'role': role, 'reference': ref})
                continue
            selected.add(target)
            uses.setdefault(target, []).append({'material': name, 'role': role, 'source': mat['source']})
    return entries, sorted(selected), uses, {
        'archives_scanned': len(archives), 'materials': selected_materials, 'decal_definitions': definitions,
        'unresolved_references': unresolved, 'parse_warnings': warnings,
    }


def gallery(output, items, summary):
    data = json.dumps(items, ensure_ascii=True).replace('<', '\\u003c')
    page = HTML.replace('__DATA__', data).replace('__COUNT__', str(len(items)))
    (output/'index.html').write_text(page, encoding='utf-8')
    (output/'manifest.json').write_text(json.dumps({'summary': summary, 'textures': items}, indent=2), encoding='utf-8')
    with (output/'textures.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=['id', 'name', 'channel', 'category', 'source', 'path', 'width', 'height', 'original'])
        writer.writeheader()
        writer.writerows({k: row.get(k, '') for k in writer.fieldnames} for row in items)


def contact_sheets(output, items):
    candidates = [r for r in items if r['category'] == 'Rock / ground' and r['channel'] == 'Color' and r.get('preview')]
    seen = set()
    candidates = [r for r in candidates if r['sha256'] not in seen and not seen.add(r['sha256'])]
    dest = output/'contact-sheets'
    dest.mkdir(exist_ok=True)
    for start in range(0, len(candidates), 30):
        group = candidates[start:start+30]
        sheet = Image.new('RGB', (1500, 6*224), '#e7e4de')
        draw = ImageDraw.Draw(sheet)
        for i, row in enumerate(group):
            x, y = (i%5)*300, (i//5)*224
            with Image.open(output/row['preview']) as im:
                im = im.convert('RGBA'); im.thumbnail((288, 180))
                sheet.paste(im, (x+(300-im.width)//2, y+(180-im.height)//2), im)
            draw.text((x+8,y+183), row['id']+'  '+row['name'][:34], fill='#171b1d')
            draw.text((x+8,y+199), row['source'][:42], fill='#494d4c')
        sheet.save(dest/f'rock-ground-{start//30+1:02}.jpg', quality=93)
    return len(candidates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--content', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('build/beamng-decal-catalog'))
    args = parser.parse_args()
    output = args.output.resolve()
    for name in ('originals', 'previews', 'thumbs'):
        (output/name).mkdir(parents=True, exist_ok=True)
    entries, selected, uses, metadata = collect(args.content)
    print(f"Scanned {metadata['archives_scanned']} archives; selected {len(selected)} textures", flush=True)
    items, hashes, failures, total_bytes = [], {}, [], 0
    cache = {}
    if (output/'manifest.json').is_file():
        old = json.loads((output/'manifest.json').read_text(encoding='utf-8'))
        cache = {row['sha256']: row for row in old['textures']
                 if row.get('preview') and (output/row['preview']).is_file()
                 and (output/row['original']).is_file()}
    opened = {}
    try:
        for number, key in enumerate(selected, 1):
            archive, name, path = entries[key][0]
            z = opened.setdefault(archive, None)
            if z is None: z = opened[archive] = zipfile.ZipFile(archive)
            blob = z.read(name)
            digest = hashlib.sha256(blob).hexdigest()
            ident = f'D{number:04}'
            filename = PurePosixPath(path).name
            short = re.sub(r'[^\w.\-]', '_', filename)[:110]
            original = f'originals/{digest[:16]}_{short}'
            previous = hashes.get(digest) or cache.get(digest)
            if previous:
                original = previous['original']
            else:
                (output/original).write_bytes(blob)
            if digest not in hashes:
                total_bytes += len(blob)
            usage = uses.get(key, [])
            category_path = '/'.join(PurePosixPath(path).parts[-3:])
            category = 'Rock / ground' if ROCK_WORDS.search(category_path) else 'Other'
            if path.startswith('vehicles/'): category = 'Vehicle decals'
            if category == 'Other' and re.search('asphalt|road|tarmac|crack|pothole|repair', category_path, re.I): category = 'Road / asphalt'
            parts = PurePosixPath(path).parts
            source = parts[1] if parts[0] in ('levels', 'vehicles') else 'Shared assets'
            row = dict(id=ident, name=filename, path='/'+path, source=source, category=category,
                       channel=channel(path, [u['role'] for u in usage]), original=original,
                       archive=str(archive.relative_to(args.content)), sha256=digest,
                       materials=sorted({u['material'] for u in usage}), bytes=len(blob))
            if previous:
                for field in ('preview', 'thumb', 'width', 'height', 'alpha', 'error'):
                    if field in previous: row[field] = previous[field]
            else:
                try:
                    with preview_image(blob) as im:
                        row.update(width=im.width, height=im.height)
                        im = im.convert('RGBA')
                        row['alpha'] = im.getchannel('A').getextrema()[0] < 255
                        im.thumbnail((1536,1536), Image.Resampling.LANCZOS)
                        row['preview'] = f'previews/{digest[:16]}.webp'
                        im.save(output/row['preview'], quality=93, method=1)
                        im.thumbnail((320,240), Image.Resampling.LANCZOS)
                        row['thumb'] = f'thumbs/{digest[:16]}.webp'
                        im.save(output/row['thumb'], quality=88, method=1)
                except (OSError, ValueError, NotImplementedError) as exc:
                    row['error'] = str(exc)
                    failures.append({'path':path, 'error':str(exc)})
            hashes[digest] = row
            items.append(row)
            if number % 100 == 0: print(f'{number}/{len(selected)} decoded', flush=True)
    finally:
        for z in opened.values(): z.close()
    summary = dict(archives_scanned=metadata['archives_scanned'], textures=len(items),
                   unique_files=len(hashes), original_bytes=total_bytes, preview_failures=failures)
    summary['rock_ground_color_choices'] = contact_sheets(output, items)
    gallery(output, items, summary)
    (output/'material-definitions.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    (output/'README.txt').write_text(
        'Open index.html to browse and pick textures. It works offline without a server.\n'
        'The first view shows rock/ground color maps; choose All categories and All maps to see everything.\n'
        'Use Pick, then copy the selection IDs back into the chat.\n'
        'previews/: browser-readable images, at most 1536 px. originals/: exact native texture files.\n'
        'Contact sheets summarize rock/ground color choices. Manifest/CSV retain archive and game paths.\n'
        'Normal-map previews show stored channels; two-channel compressed normals may not look like purple RGB normals.\n'
        'Images show texture data, not the final game material tint, projection, or separate opacity map.\n'
        'These assets are local copies from the installed game, not redistributable mod assets.\n', encoding='utf-8')
    print(json.dumps(summary, indent=2), flush=True)
    print(output/'index.html', flush=True)


HTML = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BeamNG decal library</title><style>
*{box-sizing:border-box}body{margin:0;background:#ece9e2;color:#222a29;font:15px system-ui,sans-serif}header{background:#263b38;color:#fff;padding:24px 32px 18px}h1{font-size:28px;margin:0 0 7px}header p{margin:0;color:#d2dfda;max-width:960px;line-height:1.6}main{padding:22px 32px}button,input,select{font:inherit}button,select,input{border:1px solid #acb7b0;border-radius:5px;padding:9px;background:#fff;color:#233a34}button{cursor:pointer}button:hover{background:#e8f3e9}button.active{background:#2c5147;color:white}#filters{display:flex;gap:12px;flex-wrap:wrap;align-items:end}label{display:grid;gap:5px;font-size:13px;font-weight:600}#search{min-width:290px}#status{margin:18px 0;color:#56615a}#selection{margin:14px 0;display:flex;gap:8px;align-items:center}#chosen{flex:1;min-width:150px;background:#f8faf6}#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:16px}.card{background:white;border:1px solid #ccd0c7;border-radius:7px;overflow:hidden}.checker{background-color:#c6c6bf;background-image:linear-gradient(45deg,#e0e0d8 25%,transparent 25%),linear-gradient(-45deg,#e0e0d8 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#e0e0d8 75%),linear-gradient(-45deg,transparent 75%,#e0e0d8 75%);background-size:24px 24px;background-position:0 0,0 12px,12px -12px,-12px 0}.preview{width:100%;height:210px;padding:8px;border:0;border-radius:0;display:block}.preview img{width:100%;height:100%;object-fit:contain}.info{padding:13px}.info strong{display:block;font-size:13px;overflow-wrap:anywhere;line-height:1.5}.meta{font-size:12px;color:#5a685f;margin:7px 0}.actions{display:flex;gap:8px;align-items:center}.actions a{font-size:12px;color:#31594c}#more{display:block;margin:25px auto}dialog{width:min(1250px,96vw);max-height:94vh;padding:20px;border:0;border-radius:9px;background:#f4f2ed}dialog::backdrop{background:#102020c9}.modalhead{display:flex;justify-content:space-between;gap:16px;align-items:center}h2{font-size:19px;overflow-wrap:anywhere}#large{display:block;max-width:100%;max-height:65vh;margin:auto;object-fit:contain}#details{overflow-wrap:anywhere;font-size:13px;line-height:1.7}#related{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}#related button{font-size:12px}small{font-size:12px}@media(max-width:650px){header,main{padding:18px}#search{min-width:0;width:100%}#selection{flex-wrap:wrap}label{flex:1}#grid{grid-template-columns:1fr}}
</style><header><h1>BeamNG decal library</h1><p>__COUNT__ textures from the installed game. Start with rock and ground materials, or broaden the filters. Open a texture to inspect it and its related maps; pick IDs to send back to the chat.</p></header><main>
<div id="filters"><label>Search<input id="search" placeholder="rock, dirt, sand, a filename or material…" type="search"></label><label>Category<select id="category"><option>Rock / ground</option><option>All categories</option><option>Road / asphalt</option><option>Other</option><option>Vehicle decals</option></select></label><label>Map<select id="channel"><option>Color</option><option>All maps</option></select></label><label>Source<select id="source"><option>All sources</option></select></label><label><span>Duplicates</span><select id="duplicates"><option value="hide">Hide identical files</option><option value="show">Show every source</option></select></label><button id="all">Show all textures</button></div>
<div id="selection"><strong>Picked</strong><input readonly id="chosen" aria-label="Selected texture IDs" placeholder="No textures picked"><button id="copy">Copy IDs</button><button id="clear">Clear</button></div><p id="status" aria-live="polite"></p><div id="grid"></div><button id="more">Show more</button>
<p><small>Previews are limited to 1536 px; original files retain full resolution. Texture data is shown without the game's lighting/material tint or separate opacity masks. Two-channel normal maps may look yellow rather than purple. These are local game assets.</small></p></main>
<dialog id="modal"><div class="modalhead"><h2 id="title"></h2><button id="close">Close</button></div><div class="checker"><img id="large" alt=""></div><p id="details"></p><div class="actions"><button id="pickModal">Pick texture</button><a id="original" target="_blank">Original file</a><a id="full" target="_blank">Open preview</a></div><div id="related"></div></dialog>
<script>
const DATA=__DATA__;
const $=id=>document.getElementById(id),picked=new Set();let visible=[],limit=80,current=null;
const key='beamng-decal-picks-v1';try{JSON.parse(localStorage.getItem(key)||'[]').forEach(x=>picked.add(x))}catch(e){}
function selection(){ $('chosen').value=[...picked].sort().join(', ');try{localStorage.setItem(key,JSON.stringify([...picked]))}catch(e){}}
function toggle(id){picked.has(id)?picked.delete(id):picked.add(id);selection();render();if(current)$('pickModal').textContent=picked.has(current.id)?'Remove pick':'Pick texture'}
function option(parent,value){const e=document.createElement('option');e.textContent=value;parent.append(e)}
[...new Set(DATA.map(x=>x.channel))].sort().filter(x=>x!=='Color').forEach(x=>option($('channel'),x));
[...new Set(DATA.map(x=>x.source))].sort().forEach(x=>option($('source'),x));
function filter(){const q=$('search').value.trim().toLowerCase(),seen=new Set();limit=80;
visible=DATA.filter(x=>{if($('category').value!=='All categories'&&x.category!==$('category').value)return false;if($('channel').value!=='All maps'&&x.channel!==$('channel').value)return false;if($('source').value!=='All sources'&&x.source!==$('source').value)return false;if(q&&!(x.id+' '+x.path+' '+x.materials.join(' ')).toLowerCase().includes(q))return false;if($('duplicates').value==='hide'&&seen.has(x.sha256))return false;seen.add(x.sha256);return true});render()}
function render(){const grid=$('grid');grid.replaceChildren();for(const x of visible.slice(0,limit)){const c=document.createElement('article');c.className='card';const b=document.createElement('button');b.className='preview checker';b.setAttribute('aria-label','Inspect '+x.id+' '+x.name);b.onclick=()=>open(x);if(x.thumb){const im=document.createElement('img');im.src=x.thumb;im.alt=x.name;im.loading='lazy';b.append(im)}else b.textContent='Preview unavailable — original retained';c.append(b);const info=document.createElement('div');info.className='info';const title=document.createElement('strong');title.textContent=x.id+' · '+x.name;const meta=document.createElement('p');meta.className='meta';meta.textContent=x.source+' · '+x.channel+' · '+(x.width?x.width+' × '+x.height:'native file');const actions=document.createElement('div');actions.className='actions';const pick=document.createElement('button');pick.textContent=picked.has(x.id)?'Picked ✓':'Pick';pick.className=picked.has(x.id)?'active':'';pick.onclick=()=>toggle(x.id);const link=document.createElement('a');link.href=x.original;link.textContent='Original';link.target='_blank';actions.append(pick,link);info.append(title,meta,actions);c.append(info);grid.append(c)}$('status').textContent=visible.length+' matches · showing '+Math.min(limit,visible.length)+' · '+DATA.length+' total source textures';$('more').hidden=limit>=visible.length}
function open(x){current=x;$('title').textContent=x.id+' · '+x.name;$('large').src=x.preview||'';$('large').hidden=!x.preview;$('large').alt=x.name;$('details').textContent=x.path+' | Archive: '+x.archive+' | Materials: '+(x.materials.join(', ')||'unassigned/library texture')+(x.error?' | '+x.error:'');$('original').href=x.original;$('full').href=x.preview||x.original;$('pickModal').textContent=picked.has(x.id)?'Remove pick':'Pick texture';$('related').replaceChildren();const folder=x.path.slice(0,x.path.lastIndexOf('/'));let peers=DATA.filter(y=>y.id!==x.id&&y.path.slice(0,y.path.lastIndexOf('/'))===folder);if(x.materials.length)peers=DATA.filter(y=>y.id!==x.id&&y.materials.some(m=>x.materials.includes(m)));const unique=new Set();for(const y of peers.filter(y=>!unique.has(y.sha256)&&unique.add(y.sha256)).slice(0,50)){const b=document.createElement('button');b.textContent=y.id+' · '+y.channel+' · '+y.name;b.onclick=()=>open(y);$('related').append(b)}if(!$('modal').open)$('modal').showModal()}
for(const id of ['search','category','channel','source','duplicates'])$(id).addEventListener('input',filter);
$('all').onclick=()=>{$('category').value='All categories';$('channel').value='All maps';$('source').value='All sources';$('search').value='';filter()};$('more').onclick=()=>{limit+=80;render()};$('close').onclick=()=>$('modal').close();$('pickModal').onclick=()=>toggle(current.id);$('clear').onclick=()=>{picked.clear();selection();render()};$('copy').onclick=async()=>{$('chosen').select();try{await navigator.clipboard.writeText($('chosen').value);$('copy').textContent='Copied';setTimeout(()=>$('copy').textContent='Copy IDs',1400)}catch(e){document.execCommand('copy')}};
selection();filter();
</script></html>'''


if __name__ == '__main__':
    main()
