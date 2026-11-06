// Small offline library + deterministic synthetic fixture; no NDW or nationwide downloader.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {collada,plate} from './nl_sign_mesh.mjs';
import {linkCatalog} from './link_nl_catalog.mjs';
import {extraSigns} from './nl_extra_signs.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const src=path.join(root,'tools/nl_test_source'),out=path.join(root,'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/nl_testset');
const vfs='/levels/beamng_earth/art/shapes/beamngEarth/nl_testset/';
fs.mkdirSync(out,{recursive:true});
const write=(n,s)=>fs.writeFileSync(path.join(out,n),s),json=x=>JSON.stringify(x,null,2)+'\n';
const materials={},signs={};
for(const code of ['A1-50','B6','C1','J16',...Object.keys(extraSigns)]) {
 const circular=code==='A1-50'||code==='C1',outline=JSON.parse(fs.readFileSync(path.join(src,code+'.outline.json')));
 const contour=circular?Array.from({length:48},(_,i)=>[.5+.5*Math.cos(i*Math.PI/24),.5-.5*Math.sin(i*Math.PI/24)]):outline.contour;
 const extra=extraSigns[code],width=extra?.width_m??(circular?.6:.7),height=extra?.height_m??(circular?.6:.7*outline.aspect),mat='bne_nl_test_'+code.replaceAll('-','_');
 const mesh=plate(contour,width,height,mat);
 write(code+'.dae',collada(code.replace('-','_'),mesh.positions,mesh.triangles,code+'.color.png',mesh.uvs));
 const texture=fs.readFileSync(path.join(src,code+'.color.png'));write(code+'.color.png',texture);
 materials[mat]={name:mat,mapTo:mat,class:'Material',version:1.5,Stages:[{baseColorMap:vfs+code+'.color.png',metallicFactor:0,roughnessFactor:.65}],doubleSided:false};
 signs[code]={code,width_m:width,height_m:height,shape:circular?'circle':code==='B6'?'inverted_rounded_triangle':'rounded_triangle',model:code+'.dae',texture:code+'.color.png',material:mat,vertices:mesh.positions.length,triangles:mesh.triangles.length,plate_thickness_m:.003,rear_return_m:.012,
  source:'https://commons.wikimedia.org/wiki/File:Nederlands_verkeersbord_'+(code==='A1-50'?'A1':code)+'_2023.svg',author:'Bouwe Brouwer',license:'Public domain; PD Dutch traffic signs',svg_sha256:createHash('sha256').update(fs.readFileSync(path.join(src,code+'.svg'))).digest('hex'),texture_sha256:createHash('sha256').update(texture).digest('hex'),status:'source and offline geometry checked; in-game acceptance varies by asset',...extra};
}
materials.bne_nl_test_metal={name:'bne_nl_test_metal',mapTo:'bne_nl_test_metal',class:'Material',version:1.5,Stages:[{baseColorFactor:[.56,.58,.60,1],metallicFactor:.7,roughnessFactor:.65}],doubleSided:false};
const poles=[];
for(const [cm,total,above] of [[300,3,2.2],[330,3.3,2.5],[360,3.6,2.8]]) {
 const name='NL_flespaal_'+cm+'cm.dae';
 write(name,fs.readFileSync(path.join(src,name),'utf8').replaceAll('zinc_mat','bne_nl_test_metal').replace('name="zinc"','name="bne_nl_test_metal"'));
 poles.push({model:name,total_m:total,source_above_m:above,source_buried_m:.8,vertices:32,triangles:60,lower_diameter_m:.076,upper_diameter_m:.048});
}
// Reuse two copies of the supplied clamp at +/-14 cm; no bolts/decorative geometry.
const clamp=fs.readFileSync(path.join(src,'NL_flespaal_beugel_48mm.dae'),'utf8');
const floats=clamp.match(/<float_array[^>]*>([^<]+)/)[1].trim().split(/\s+/).map(Number),idx=clamp.match(/<p>([^<]+)/)[1].trim().split(/\s+/).map(Number);
const cp=[],ct=[];
for(const dz of [-.14,.14]) {const offset=cp.length;for(let i=0;i<floats.length;i+=3)cp.push([floats[i],floats[i+1],floats[i+2]+dz]);for(let i=0;i<idx.length;i+=3)ct.push(['bne_nl_test_metal',...idx.slice(i,i+3).map(i=>i+offset)]);}
write('clamps_28cm.dae',collada('clamps_28cm',cp,ct));
// Short pair for 20 cm supplementary plates: keep clamps inside the plate height.
const shortP=[],shortT=[];
for(const dz of [-.05,.05]){const offset=shortP.length;for(let i=0;i<floats.length;i+=3)shortP.push([floats[i],floats[i+1],floats[i+2]+dz]);for(let i=0;i<idx.length;i+=3)shortT.push(['bne_nl_test_metal',...idx.slice(i,i+3).map(i=>i+offset)]);}
write('clamps_10cm.dae',collada('clamps_10cm',shortP,shortT));
const tinyP=[],tinyT=[];for(const dz of [-.015,.015]){const offset=tinyP.length;for(let i=0;i<floats.length;i+=3)tinyP.push([floats[i],floats[i+1],floats[i+2]+dz]);for(let i=0;i<idx.length;i+=3)tinyT.push(['bne_nl_test_metal',...idx.slice(i,i+3).map(i=>i+offset)]);}
write('clamps_3cm.dae',collada('clamps_3cm',tinyP,tinyT));
const assemblies=[];
for(const [row,code] of ['A1-50','B6','C1','J16'].entries())for(let col=0;col<10;col++) {
 if(row===0&&col===0)continue; // That speed sign shares the first C1 post instead.
 const top=(row===2&&col===0)?2.65:[2.1,2.35,2.65][col%3],centre=top-signs[code].height_m/2;
 const boards=[{code,centre_m:centre}];if(row===2&&col===0)boards.push({code:'A1-50',centre_m:centre-.68});
 const poleTop=top-.05,valid=p=>p.total_m-poleTop>=.6&&p.total_m-poleTop<=1.1;
 const pole=valid(poles[col%3])?poles[col%3]:poles.find(valid);
 if(!pole)throw Error('No suitable source pole');
 assemblies.push({id:`trial_${row}_${col}`,x_m:col*4,y_m:row*7,face_yaw_deg:-90,boards,pole:pole.model,pole_offset_m:poleTop-pole.source_above_m,pole_top_m:poleTop,buried_m:pole.total_m-poleTop});
}
const counts={};for(const a of assemblies)for(const b of a.boards)counts[b.code]=(counts[b.code]||0)+1;
const triangles=assemblies.reduce((s,a)=>s+60+a.boards.reduce((s,b)=>s+signs[b.code].triangles+ct.length,0),0);
write('main.materials.json',json(materials));
write('manifest.json',json({schema_version:1,vfs,fixture:'synthetic; 40 signs, 4 representative codes, not surveyed NDW positions',signs,poles,clamps:{model:'clamps_28cm.dae',triangles:ct.length,vertices:cp.length,spacing_m:.28},assemblies,counts,metrics:{signs:40,posts:39,clamp_pairs:40,tsstatic_objects:119,triangles,materials:5,distinct_models:8,collision:'None'},library_counts:{sign_variants:Object.keys(signs).length,materials:Object.keys(materials).length,clamp_models:2},missing:['Every RVV variant except the exact keys in signs','NDW import and owner dataset','Automatic road-side selection and surveyed heading/mounting','Forest comparison, GPU/total process memory and real FPS measurement','Double-pole structures and national rollout'],status:'small test fixture; in-game acceptance and benchmark pending'}));
console.log(`Generated testset: ${assemblies.length} assemblies, ${JSON.stringify(counts)}, ${triangles} triangles; no national data.`);

linkCatalog(out,path.join(root,'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/nl_catalog'));
