import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const out=path.join(root,'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/nl_testset');
const manifest=JSON.parse(fs.readFileSync(path.join(out,'manifest.json'))),mats=JSON.parse(fs.readFileSync(path.join(out,'main.materials.json')));
const actual={};let triangles=0;
for(const a of manifest.assemblies) {
 assert.ok(a.buried_m>=.6&&a.buried_m<=1.1);
 const pole=manifest.poles.find(p=>p.model===a.pole);
 const top=Math.max(...a.boards.map(b=>b.centre_m+manifest.signs[b.code].height_m/2));
 assert.ok(Math.abs(top-.05-a.pole_top_m)<1e-8);
 assert.ok(Math.abs(pole.source_above_m+a.pole_offset_m-a.pole_top_m)<1e-8);
 triangles+=60;
 for(const b of a.boards){actual[b.code]=(actual[b.code]||0)+1;triangles+=manifest.signs[b.code].triangles+manifest.clamps.triangles;
 assert.ok(b.centre_m+.14+.018<a.pole_top_m);}
}
assert.deepEqual(actual,{'A1-50':10,B6:10,C1:10,J16:10});
assert.equal(manifest.assemblies.length,39);assert.equal(triangles,manifest.metrics.triangles);
assert.equal(new Set(manifest.assemblies.map(a=>a.pole)).size,3);
assert.equal(manifest.assemblies.filter(a=>a.boards.length===2).length,1);
const nums=s=>s.trim().split(/\s+/).map(Number);
for(const [code,sign] of Object.entries(manifest.signs)) {
 if(sign.catalog_source)continue; // bulk catalogue is checked by its separate asset test
 const png=fs.readFileSync(path.join(out,sign.texture));
 assert.equal(png.readUInt32BE(16),1024);assert.equal(png.readUInt32BE(20),1024);assert.equal(png[24],8);assert.equal(png[25],2);
 assert.equal(mats[sign.material].Stages[0].baseColorMap,manifest.vfs+sign.texture);
 const xml=fs.readFileSync(path.join(out,sign.model),'utf8'),p=nums(xml.match(/<float_array id="pos_a"[^>]*>([^<]+)/)[1]);
 assert.equal(p.length/3,sign.vertices);
 const edges=new Map();let count=0;
 for(const m of xml.matchAll(/<triangles material="([^"]+)" count="(\d+)">[\s\S]*?<p>([^<]+)/g)) {
  const ind=nums(m[3]);assert.equal(ind.length,Number(m[2])*9);
  for(let i=0;i<ind.length;i+=9) {
   const abc=[ind[i],ind[i+3],ind[i+6]];count++;
   assert.ok(abc.every(i=>i>=0&&i<sign.vertices));
   const [A,B,C]=abc.map(i=>p.slice(i*3,i*3+3)),u=B.map((x,k)=>x-A[k]),v=C.map((x,k)=>x-A[k]);
   const cross=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]];
   assert.ok(Math.hypot(...cross)>1e-12);
   if(m[1]===sign.material+'_symbol')assert.ok(cross[1]<0);
   for(let k=0;k<3;k++) {const a=abc[k],b=abc[(k+1)%3],key=[a,b].sort((a,b)=>a-b).join(',');const e=edges.get(key)||[0,0];e[0]++;e[1]+=a<b?1:-1;edges.set(key,e);}
  }
 }
 assert.equal(count,sign.triangles);assert.ok([...edges.values()].every(([n,w])=>n===2&&w===0),'Closed consistently wound plate '+code);
}
const floats=xml=>xml.match(/<float_array[^>]*>[\s\S]*?<\/float_array>/g);
for(const p of manifest.poles)assert.deepEqual(floats(fs.readFileSync(path.join(out,p.model),'utf8')),floats(fs.readFileSync(path.join(root,'tools/nl_test_source',p.model),'utf8')));
const shortXml=fs.readFileSync(path.join(out,'clamps_10cm.dae'),'utf8');
const shortPos=nums(shortXml.match(/<float_array id="pos_a"[^>]*>([^<]+)/)[1]);
assert.ok(shortPos.filter((_,i)=>i%3===2).every(z=>Math.abs(z)<.1),'Short clamps stay inside the 20cm plate');
assert.equal(manifest.signs.OB504.clamp_model,'clamps_10cm.dae');
assert.equal(manifest.signs.OB504.role,'supplementary');
assert.equal(manifest.signs['A1-30-ZB'].zone_code,'ZB');
assert.equal(manifest.signs['A2-30-ZE'].rvv_code,'A2');
const hashes=()=>Object.fromEntries(fs.readdirSync(out).sort().map(n=>[n,createHash('sha256').update(fs.readFileSync(path.join(out,n))).digest('hex')]));
const before=hashes();execFileSync(process.execPath,[path.join(root,'tools/generate_nl_testset.mjs')]);assert.deepEqual(hashes(),before);
console.log('PASS: counts, 3 pole lengths, shared post, mounting clearances, authentic RGB texture format, material paths, closed plate/rim meshes, front winding, original pole geometry, deterministic build.');
const args=process.argv.slice(2),fengari=args.indexOf('--fengari'),lua=args.indexOf('--lua-bin');
if(fengari>=0||lua>=0) {
 const literal=x=>x===null?'nil':Array.isArray(x)?'{'+x.map(literal).join(',')+'}':typeof x==='object'?'{'+Object.entries(x).map(([k,v])=>'['+JSON.stringify(k)+']='+literal(v)).join(',')+'}':JSON.stringify(x);
 const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'nl-sign-test-')),fixture=path.join(tmp,'fixture.lua');
 try {fs.writeFileSync(fixture,'return '+literal({...manifest,signs:Object.fromEntries(Object.entries(manifest.signs).filter(([,s])=>!s.catalog_source))}));const test=path.join(root,'mod/tests/test_nl_testset.lua');
 const result=fengari>=0?execFileSync(process.execPath,[path.resolve(args[fengari+1]),test,fixture],{encoding:'utf8'}):execFileSync(args[lua+1],[test,fixture],{encoding:'utf8'});assert.ok(result.includes('PASS: 119 objects'), 'Lua runner did not report successful completion: '+result);console.log(result.trim());}
 finally {fs.unlinkSync(fixture);fs.rmdirSync(tmp);}
}
