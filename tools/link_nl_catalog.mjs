import fs from 'node:fs';import path from 'node:path';
export function canonicalSiteCode(code){return code.toUpperCase().replace(/^([A-Z]+)0*(\d+)/,(_,a,n)=>a+Number(n)).replace(/-(\d+)/g,(_,n)=>'-'+Number(n)).replace(/-?(ZB|ZE|ZH|ZO)$/,'-$1');}
export function linkCatalog(base,cat){
 const file=path.join(cat,'manifest.json');if(!fs.existsSync(file))return;
 const m=JSON.parse(fs.readFileSync(path.join(base,'manifest.json'))),c=JSON.parse(fs.readFileSync(file)),vfs='/levels/beamng_earth/art/shapes/beamngEarth/nl_catalog/';
 let added=0;for(const e of c.entries){if(e.status!=='generated_requires_visual_acceptance'||e.parts.length!==1)continue;const key=canonicalSiteCode(e.code),p=e.parts[0];if(m.signs[key])continue;
 m.signs[key]={code:key,model:vfs+p.model,texture:vfs+p.texture,material:p.material,material_file:vfs+(p.material_file||p.model.replace('.dae','.materials.json')),width_m:p.width_m,height_m:p.height_m,triangles:p.triangles,vertices:p.vertices,plate_thickness_m:.003,rear_return_m:.012,role:key.startsWith('OB')?'supplementary':'primary',clamp_model:p.height_m<.15?'clamps_3cm.dae':p.height_m<.3?'clamps_10cm.dae':undefined,texture_size:512,catalog_source:true,source:e.source_page,source_sha256:e.source_sha256,status:'raster-derived; generated geometry checked, in-game visual acceptance pending',requires_value:e.parameterized||/^(H|K|E8|E9)/.test(key)};added++;}
 m.catalog_materials_file=vfs+'main.materials.json';m.catalog_probe=c.entries.find(e=>e.parts.length)?.parts[0].material;m.catalog_summary={source_entries:c.summary.entries,single_plate_aliases_added:added,...c.summary};
 fs.writeFileSync(path.join(base,'manifest.json'),JSON.stringify(m,null,2)+'\n');
 return added;
}
