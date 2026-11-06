// Classify existing plate outlines; no texture/mesh regeneration or runtime model reads.
import fs from 'node:fs';import path from 'node:path';import{fileURLToPath}from'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),mod=path.join(root,'mod/beamng_earth');
const base=path.join(mod,'levels/beamng_earth/art/shapes/beamngEarth/nl_testset');
const manifest=JSON.parse(fs.readFileSync(path.join(base,'manifest.json'))),shapes={},counts={};
for(const[code,s]of Object.entries(manifest.signs)){
 let shape=s.shape==='circle'?'circle':/triangle/.test(s.shape||'')?'triangle':null;
 if(!shape){
  const model=s.model.startsWith('/')?path.join(mod,s.model.slice(1)):path.join(base,s.model);
  const xml=fs.readFileSync(model,'utf8'),match=xml.match(/<source id="pos"><float_array[^>]*>([^<]+)</);
  if(!match)throw Error('Unknown position layout: '+code);
  const p=match[1].trim().split(/\s+/).map(Number),n=(p.length/3-2)/4;
  if(!Number.isInteger(n)||n<3)throw Error('Unknown plate layout: '+code);
  const points=Array.from({length:n},(_,i)=>[p[(i+2)*3],p[(i+2)*3+2]]);
  const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]),w=Math.max(...xs)-Math.min(...xs),h=Math.max(...ys)-Math.min(...ys);
  const radii=points.map(([x,y])=>Math.hypot(x/w,y/h));
  const area=Math.abs(points.reduce((a,p,i)=>{const q=points[(i+1)%n];return a+p[0]*q[1]-q[0]*p[1]},0))/2/(w*h);
  // Rounded-square hulls may contain only corner vertices, all at a similar
  // radius (~0.68). A circle must also lie at radius ~0.5 of its bounding box.
  if(n>=12&&Math.abs(h/w-1)<.04&&Math.min(...radii)>.46&&Math.max(...radii)<.54&&Math.max(...radii)/Math.min(...radii)<1.07)shape='circle';
  else if(/^(J\d|B6(?:$|-))/.test(code)&&h/w>.8&&h/w<.96&&area>.43&&area<.58)shape='triangle';
 }
 if(s.role==='supplementary')shape=null;
 if(shape)shapes[code]=shape;
 counts[shape||'special_unresolved']=(counts[shape||'special_unresolved']||0)+1;
}
const result={schema_version:1,kind:'nl_plate_size_profiles',shapes,counts,policy:'Only recognised round/triangular plates; special formats stay unresolved. Road-selected dimensions are inferred, not surveyed.'};
fs.writeFileSync(path.join(base,'size_profiles.json'),JSON.stringify(result,null,2)+'\n');
console.log(JSON.stringify(counts));
