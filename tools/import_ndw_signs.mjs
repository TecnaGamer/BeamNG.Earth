import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
let registry;export function assetRegistry(){return registry??=JSON.parse(fs.readFileSync(new URL('../mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/nl_testset/manifest.json',import.meta.url))).signs;}
export function tileKey(lat,lon){const n=2**17,a=lat*Math.PI/180;return `g17_${Math.floor((lon+180)/360*n)}_${Math.floor((1-Math.log(Math.tan(a)+1/Math.cos(a))/Math.PI)/2*n)}`;}
export function normalizeSubplates(value){
 if(value===undefined)return [];
 if(!Array.isArray(value))return [{status:'invalid_subplate_array',raw:value}];
 return value.map(r=>{
  if(!r||typeof r!=='object')return {status:'invalid_subplate',raw:r};
  const type=String(r.type??'').trim().toUpperCase(),text=String(r.text??'').trim().replace(/\s+/g,' '),opening_hours=r.openingHours??null;
  if(!text)return {type,text,opening_hours,status:'missing_text_or_symbol'};
  if(/^(uitgezonderd|except)$/i.test(text))return {type,text,opening_hours,status:'exception_target_missing'};
  const match=type==='VOOR'&&text.match(/^(\d+(?:[.,]\d+)?)\s*(m|km)$/i);
  if(match)return {type,text,opening_hours,distance_m:Number(match[1].replace(',','.'))*(match[2].toLowerCase()==='km'?1000:1),status:'exact_distance_artwork_missing'};
  return {type,text,opening_hours,status:'exact_artwork_missing'};
 });
}
export function normalize(feature){
 const p=feature?.properties||{},c=feature?.geometry?.coordinates;
 const reject=(reason,details)=>({id:feature?.id||null,reason,rvv_code:p.rvvCode||null,...(details?{details}:{})});
 if(typeof feature?.id!=='string'||!feature.id.trim())return reject('missing_id');
 if(feature.geometry?.type!=='Point'||!Array.isArray(c)||c.length<2||!c.slice(0,2).every(Number.isFinite)||c[0]<3||c[0]>8||c[1]<50||c[1]>54)return reject('invalid_wgs84_point');
 if(p.status!=='PLACED')return reject('not_placed');
 const code=String(p.rvvCode||'').trim().toUpperCase(),black=String(p.blackCode??'').trim(),zone=String(p.zoneCode??'').trim();
 const normalizedCode=code.replace(/\(ZONE\)$/,'');
 let asset;
 if(zone||code.includes('ZONE')){
  asset=black==='30'&&zone==='ZB'&&normalizedCode==='A1'?'A1-30-ZB':black==='30'&&zone==='ZE'&&normalizedCode==='A2'?'A2-30-ZE':null;
  if(!asset){const key=normalizedCode+(black?'-'+black.replace(',','.'): '')+'-'+zone;if(assetRegistry()[key])asset=key;}
  if(!asset)return reject('zone_asset_missing');
 }else asset=code==='A1'&&black==='50'?'A1-50':['B6','C1','J16'].includes(code)&&!black?code:null;
 const subplates=normalizeSubplates(p.textSigns);
 if(subplates.length)return reject('supplementary_plate_not_supported',subplates);
 if(!asset){const key=code+(black?'-'+black.replace(',','.'): '');const candidate=assetRegistry()[key];if(candidate&&(!candidate.requires_value||black))asset=key;}
 // George keeps directional variants in blackCode; the catalogue appends them
 // without a dash. D2 uses LO/RO (links/rechts onder), not L/R.
 // Keep this explicit: arbitrary source values must never select generic art.
 if(!asset&&!zone&&!code.includes('ZONE')){
  const direction=black.toUpperCase();
  const suffix=code==='D2'?({LO:'L',RO:'R'}[direction]):/^(C4|D5|D6)$/.test(code)&&/^[LR]$/.test(direction)?direction:null;
  if(suffix&&assetRegistry()[code+suffix])asset=code+suffix;
 }
 if(!asset)return reject('asset_missing');
 if(p.placement!=='L')return reject('mounting_not_roadside');
 if(typeof p.bearing!=='number'||!Number.isInteger(p.bearing)||p.bearing<0||p.bearing>=360)return reject('bearing_missing_or_invalid');
 return {id:feature.id,lat:c[1],lon:c[0],asset,rvv_code:code,black_code:black||null,zone_code:zone||null,text_signs:p.textSigns||[],bearing_deg:p.bearing,orientation_status:'source_bearing_preserved_pending_visual_confirmation',nen_turning_direction:p.nenTurningDirection??null,side:p.side??null,validated:p.validated??null,last_seen_on:p.lastSeenOn??null,road_name:p.roadName??null,road_section_id:p.roadSectionId??null,tile:tileKey(c[1],c[0]),mounting:{status:'preview_assumption_not_surveyed',board_top_m:2.35,pole_top_clearance_m:.05,post_count:1}};
}
export function importArea(input,{bbox,limit=40}){
 if(!Array.isArray(bbox)||bbox.length!==4||!bbox.every(Number.isFinite)||bbox[0]>=bbox[2]||bbox[1]>=bbox[3]||bbox[2]-bbox[0]>.02||bbox[3]-bbox[1]>.02)throw Error('Use a valid small WGS84 bbox west,south,east,north (maximum .02 degrees each axis)');
 if(!Number.isInteger(limit)||limit<1||limit>40)throw Error('Pilot limit must be 1..40');
 if(input?.type!=='FeatureCollection'||!Array.isArray(input.features))throw Error('Expected NDW WGS84 GeoJSON FeatureCollection');
 if(input.crs&&!/CRS84|4326/.test(JSON.stringify(input.crs)))throw Error('Unsupported CRS; request WGS84');
 const selected=input.features.filter(f=>{const c=f.geometry?.coordinates;return Array.isArray(c)&&c[0]>=bbox[0]&&c[0]<=bbox[2]&&c[1]>=bbox[1]&&c[1]<=bbox[3]});
 const ids=new Map();for(const f of selected)ids.set(f.id,(ids.get(f.id)||0)+1);
 const eligible=[],rejected=[];
 for(const f of selected){const n=normalize(f);if(ids.get(f.id)>1)rejected.push({id:f.id,reason:'duplicate_source_id'});else if(n.reason)rejected.push(n);else eligible.push(n);}
 const cx=(bbox[0]+bbox[2])/2,cy=(bbox[1]+bbox[3])/2,dist=r=>(r.lon-cx)**2*Math.cos(cy*Math.PI/180)**2+(r.lat-cy)**2;
 eligible.sort((a,b)=>dist(a)-dist(b)||a.id.localeCompare(b.id,'en'));const records=eligible.slice(0,limit);
 for(const n of eligible.slice(limit))rejected.push({id:n.id,reason:'pilot_limit'});
 const tiles={};for(const n of records)(tiles[n.tile]??=[]).push(n);
 const reasons={};for(const n of rejected)reasons[n.reason]=(reasons[n.reason]||0)+1;
 return {schema_version:1,kind:'ndw_small_area_preview',bbox,limit,records,tiles,report:{source_features:input.features.length,area_features:selected.length,accepted:records.length,rejected:rejected.length,reasons,issues:rejected.sort((a,b)=>String(a.id).localeCompare(String(b.id),'en')||a.reason.localeCompare(b.reason))},warnings:['Source locations are not surveyed mounting positions.','No grouping of nearby signs into inferred shared posts.','Source bearing is preserved; renderer requires explicit convention confirmation.','No national activation.']};
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 const [file,out,box]=process.argv.slice(2);if(!file||!out||!box)throw Error('Usage: node tools/import_ndw_signs.mjs input.geojson output-dir west,south,east,north');
 if(fs.statSync(file).size>50*1024*1024)throw Error('Use an API county subset, maximum 50 MB; do not load the national file');
 const bytes=fs.readFileSync(file),result=importArea(JSON.parse(bytes),{bbox:box.split(',').map(Number)});
 result.source={provider:'NDW traffic signs current-state API v4',sha256:createHash('sha256').update(bytes).digest('hex'),documentation:'https://docs.ndw.nu/data-uitwisseling/interface-beschrijvingen/verkeersborden-api/'};
 fs.mkdirSync(out,{recursive:true});fs.writeFileSync(path.join(out,'index.json'),JSON.stringify(result,null,2)+'\n');
 for(const [key,records]of Object.entries(result.tiles))fs.writeFileSync(path.join(out,key+'.json'),JSON.stringify({schema_version:1,key,records},null,2)+'\n');
 console.log(JSON.stringify({...result.report,issues:undefined}));
}
