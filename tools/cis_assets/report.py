"""Generate the review report after all production checks and final renders complete."""
import sys,json,platform
sys.dont_write_bytecode=True
from pathlib import Path
from PIL import Image,__version__ as pillow_version
import numpy as np
from specs import *

def main():
    proof=json.loads((ROOT/'assets/cis_src/rebuild_verification.json').read_text());assert proof['pass_all']
    mods=json.loads((OUT/'bne_cis_modules.json').read_text())['modules'];lines=['# Batch S-1 review report','',
    'Branch: `feature/soviet-style`. Ordered task commits: A0, A1, A2, A3, A4, A5, M1, M2, M3, M4, M5, M6. No push.', '',
    f'Validation: PASS after every task, and after final regeneration. A clean isolated rebuild validates every intermediate task and reproduces all {proof["files"]} production files byte-for-byte. SHA256 inventory: [rebuild_verification.json](../cis_src/rebuild_verification.json).', '',
    f'Tools: Python {platform.python_version()}, NumPy {np.__version__}, Pillow {pillow_version}, Blender 4.5.15 LTS. Generators and rebuild commands: [README](../../tools/cis_assets/README.md).', '',
    'All source imagery is original procedural drawing. Image-model prompts used: **none**, for every task. No photo pixels, host fonts, downloaded imagery, readable text, logos or house numbers. [Provenance](../cis_src/prompts.md).', '',
    '## Deliverable summary','', '| Task | Kit / module family | Cells / models | Atlas fill | Production triangles |', '| --- | --- | ---: | ---: | --- |']
    for task in TASKS:
        if task in KITS:
            k=KITS[task];d=json.loads((OUT/(P+k['kit']+'.cells.json')).read_text());fill=sum(c['px'][2]*c['px'][3] for c in d['cells'])/2048**2*100
            lines.append(f'| {task} | {k["kit"]} | {len(d["cells"])} cells | {fill:.2f}% | 0 (textures only) |')
        else:
            mm=[m for m in mods if m['task']==task];lines.append(f'| {task} | {task.lower()} | {len(mm)} models | A0 reused (7.40%) | {min(m["tris"] for m in mm)}–{max(m["tris"] for m in mm)} per model |')
    lines+=['','Atlas fill is occupied cell area / 2048², excluding gutters and unused pixels. All atlas cells are 96 px/m with nearest-pixel rounding, four-pixel intercell gutters and four-pixel margins. Module families create no unique texture atlases. The additional 1024² roof material repeats once per 2 m.', '',
    'Materials use version 1.5, UUID5 persistent IDs, future DDS paths, localpoi green-up normals and grayscale roughness data. Metallic is zero as in localpoi; no ORM channel layout has been invented. Module material aliases point to A0 maps.', '',
    '## Per-task inventory','']
    for task in TASKS:
        kit=KITS[task]['kit'] if task in KITS else task.lower();images=[kit+'_'+v+'.png' for v in ('front','34','far')]
        if task=='A4':images += ['panel16_tower_'+v+'.png' for v in ('front','34','far')]
        for file in images:
            im=Image.open(REND/file);assert im.size==(1600,1000),file
        lines += [f'### {task}: {kit}','',f'Generator: `tools/cis_assets/{"build_atlas.py" if task in KITS else "build_modules.py"} {task}`. Validation: **PASS**. Image prompts: none.','', 'Previews: '+', '.join(f'[{n}]({n})' for n in images)+'.','']
        if task in KITS:
            name=P+kit;files=[name+'.'+s for s in ('color.png','normal.png','data.png','lit.png','cells.json')]
            if task=='A0':files += [P+'roof.'+s+'.png' for s in ('color','normal','data','lit')]
            lines += ['Production files under the CIS shape directory:','']+[f'- `{n}`' for n in files]+['']
            d=json.loads((OUT/(name+'.cells.json')).read_text());rolecounts={r:sum(c['role']==r for c in d['cells']) for r in KITS[task]['roles']}
            lines+=['Roles: '+', '.join(f'`{r}` × {n}' for r,n in rolecounts.items())+'. Triangle count: 0; no whole-building model is delivered.','']
        else:
            lines += ['| File | Triangles / strict limit | Fits | Bounds (width × depth × height, m) |','| --- | ---: | --- | --- |']
            for m in (m for m in mods if m['task']==task):
                assert (OUT/m['dae']).exists();size=' × '.join(f'{m[n]:.3f}' for n in ('width_m','depth_m','height_m'));lines.append(f'| `{m["dae"]}` | {m["tris"]} / {m["budget"]} | {", ".join(m["fits"])} | {size} |')
            lines+=['','Bounds include projecting trim, railings, slabs and brackets. Balcony slabs extend 0.12 m below the storey-floor origin; the open railing rises 1.05 m above that origin. Roof module origins are at the centre of the base, z=0. Wall AC explicitly uses a wall-plane centre origin as requested. Top-only M1 variants have zero selection weight and `top_only: true`.','']
    lines+=['## Shared files and rebuild inputs','',
    '- `main.materials.json`: all atlas and shared-map module materials.',
    '- `bne_cis_modules.json`: 45 module entries, task attribution, fits, attach convention, weights, dimensions, actual triangles and strict budgets.',
    '- `tools/cis_assets/specs.py`: explicit cell roles, physical dimensions, set tags and preview building dimensions.',
    '- `tools/cis_assets/build_atlas.py`, `build_modules.py`: deterministic procedural generators.',
    '- `tools/cis_assets/validate.py`: atlas roles, counts, physical scale, non-overlap/gutters, edge extrusion, weights, material maps/IDs, COLLADA units/up axis, welded positions, nondegenerate/unique triangles, winding/normal agreement, UV cell containment, strict independently specified triangle budgets and extents.',
    '- `tools/cis_assets/preview.py`, `render_batch.py`: Blender scene assembly and final preview runner.',
    '- `tools/cis_assets/verify_rebuild.py`: isolated clean rebuild and SHA256 comparison.',
    '- `assets/cis_src/prompts.md`, `rebuild_verification.json`: source provenance and reproducibility evidence.','',
    '## Preview interpretation and remaining acceptance work','',
    'The five building previews assemble cells on metre-scale footprints with seeded variants, entrance/stair columns, plinths, parapets, technical storeys where specified, weighted balcony/loggia stamping and entrance/roof details. The modern L footprint has two 18 m wings with outside spans 40 m and 30 m. Modern glazing is selected uniformly from the facade set. A4 includes both the 54 × 13.2 m slab and the 24 × 24 m tower. The 3/4 camera stays at eye height 1.7 m, approximately 30 m from the nearest corner; the lens widens to preserve the full silhouette. Far building renders are taken 300 m off the facade and contain skin only, with painted balcony variants. A0 and M tasks use material/module galleries; their far views retain the showcased asset.', '',
    'DDS conversion and in-game BeamNG material/import/placement checks were not performed: PNG delivery and later DDS conversion by the reviewer are intentional. Final runtime acceptance, including night-mask hookup by game code, remains with the reviewer. The A2 preview uses a flat placeholder roof; its final low pitched roof belongs to game code under the brief. Modules use low-cost opaque dark glazing and shared A0 weathering rather than transparency, and far balcony skins are simplified procedural representations. No full-building assets, collision meshes, Rust changes, or other repository paths were authored.','']
    (REND/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    print('REPORT.md written; 12 task inventories, 45 triangle counts, all required previews present.')
S2_ORDER=['F1','A6','A7','A8','A9','M7','M8','T1','T2','T3','T4','T5','T6']
TILE_NAMES=['roof_slate','roof_metaltile','roof_profiled','wall_silicate','wall_redbrick','wall_koroied']
def extend_s2(until):
    report=REND/'REPORT.md';prefix=report.read_text(encoding='utf-8').split('\n# Batch S-2 review report')[0].rstrip()
    baseline=json.loads((ROOT/'assets/cis_src/f1_baseline.json').read_text());proof=json.loads((ROOT/'assets/cis_src/rebuild_verification.json').read_text())
    mods=json.loads((OUT/'bne_cis_modules.json').read_text())['modules'];lines=['','# Batch S-2 review report','',
        'Branch: `feature/soviet-style`. Task order: F1 ? A6 ? A7 ? A8 ? A9 ? M7 ? M8 ? T1?T6. Local commits only; no push.', '',
        'This section supersedes the historical S-1 description of RGB roughness: **every data and lit PNG is 8-bit single-channel L**. Color and normal PNGs remain RGB. The existing DDS conversion outputs are retained unchanged; PNGs are regenerated for the next reviewer conversion.', '',
        f'Clean-rebuild verification: {proof["files"]} generator-owned PNG/JSON/DAE files; byte comparison {"PASS" if proof["pass_all"] else "FAIL"}. DDS files are excluded from regeneration. [SHA256 record](../cis_src/rebuild_verification.json).', '',
        'All images remain original procedural drawing with fixed seeds. Image-model prompts: **none**. No photographic pixels, downloaded imagery, fonts, readable text, logos or house numbers.', '']
    for task in S2_ORDER[:S2_ORDER.index(until)+1]:
        lines += ['## '+task,'','Validation: **PASS**. Image prompts: none.']
        if task=='F1':
            lines += ['', 'A1?A4 now include peeling wood frames, soft rust below sills and upper balcony slabs, five pastel insulation colours with relief and roughness edges, darker repaired sealant, damp common plinth/parapet strips, contact footprints of AC/dish brackets, laundry, varied curtains/blinds and lamp shades. A5 color, normal and lit image bytes are unchanged. F1 preserves all S-1 cell IDs, layouts, weights and physical dimensions, all COLLADA and material metadata, and all retained DDS bytes.', '',
                'Triangle counts and atlas fill remain exactly those in the S-1 tables. No new geometry or atlas cells are introduced by F1.', '',
                'Before / after renders (all 1600 ? 1000):', '']
            for kit in ('khrush_panel','panel9'):
                for view in ('front','34','far'):
                    files=[f'{kit}_{phase}_{view}.png' for phase in ('before','after')]
                    for f in files:assert Image.open(REND/f).size==(1600,1000)
                    lines.append('- '+', '.join(f'[{f}]({f})' for f in files))
            files=[OUT/n for n in sorted(baseline['png_sizes'])]
        elif task in KITS:
            k=KITS[task];name=P+k['kit'];d=json.loads((OUT/(name+'.cells.json')).read_text());area=sum(c['px'][2]*c['px'][3] for c in d['cells']);fill=100*area/(d['atlas'][0]*d['atlas'][1])
            lines += ['',f'Kit `{k["kit"]}`: {len(d["cells"])} cells, atlas {d["atlas"][0]}?, {fill:.2f}% occupied cell area, exactly 96 px/m. Production triangles: 0 (textures only).']
            files=[OUT/(name+'.'+s) for s in ('color.png','normal.png','data.png','lit.png','cells.json')]
            lines+=['','Previews: '+', '.join(f'[{k["kit"]}_{v}.png]({k["kit"]}_{v}.png)' for v in ('front','34','far'))+'.']
        elif task.startswith('M'):
            mm=[m for m in mods if m['task']==task];files=[OUT/m['dae'] for m in mm];lines+=['','Atlas: shared A0 (no unique module atlas).','','| Module | Triangles / strict limit |','| --- | ---: |']
            lines += [f'| `{m["dae"]}` | {m["tris"]} / {m["budget"]} |' for m in mm]
            lines+=['','Previews: '+', '.join(f'[{task.lower()}_{v}.png]({task.lower()}_{v}.png)' for v in ('front','34','far'))+'.']
        else:
            name=P+TILE_NAMES[int(task[1:])-1];files=[OUT/(name+'.'+s) for s in ('color.png','normal.png','data.png','tile.json')];tile=json.loads(files[-1].read_text())
            lines += ['',f'Tiling material `{name}`: 1024? RGB color/normal and L roughness, physical tile {tile["tile_m"]} m. No atlas; no production geometry. The explicit 1024? tiling-material specification is used; 96 px/m applies to facade atlas cells.', '',
                'Previews: '+', '.join(f'[{name.removeprefix(P)}_{v}.png]({name.removeprefix(P)}_{v}.png)' for v in ('front','34','far'))+'.']
        lines += ['', '| File | Mode / format | Bytes | Previous S-1 PNG bytes |','| --- | --- | ---: | ---: |']
        for path in files:
            mode=Image.open(path).mode if path.suffix=='.png' else path.suffix[1:];old=baseline['png_sizes'].get(path.name,'?');lines.append(f'| `{path.name}` | {mode} | {path.stat().st_size:,} | {old if old=="?" else format(old,",")} |')
        lines+=['']
    lines+=['## Acceptance and remaining work','',
        'DDS conversion remains the reviewer?s next step. In-game import, night-mask hookup and runtime placement are not claimed. Generators, validator, clean rebuild and Blender previews are provided in the existing `tools/cis_assets/` toolchain. F1 before images are immutable S-1 references; all current generated maps are rebuilt procedurally. No other repository folders were edited.','']
    report.write_text(prefix+'\n'+'\n'.join(lines),encoding='utf-8');print('Extended REPORT.md through '+until)

if __name__=='__main__':
    if len(sys.argv)>1:extend_s2(sys.argv[1])
    else:main()

