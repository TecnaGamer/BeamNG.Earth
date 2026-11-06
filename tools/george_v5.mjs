// NDW/George public current-state v5 adapter. No image URLs, OCR or network dependencies.
import {tileKey,assetRegistry} from './import_ndw_signs.mjs';
export function normalizeGeorge(feature,{statusFilter}={}){
 const p=feature?.properties||{},c=feature?.geometry?.coordinates;
 const fail=reason=>({id:feature?.id??null,reason});
 if(statusFilter!=='PLACED')throw Error('v5 does not export status; require provenance of a status=PLACED current-state query');
 if(feature?.type!=='Feature'||typeof feature.id!=='string'||!feature.id.trim())return fail('invalid_source_id');
 if(feature.geometry?.type!=='Point'||!Array.isArray(c)||c.length!==2||!c.every(Number.isFinite)||c[0]<3||c[0]>8||c[1]<50||c[1]>54)return fail('invalid_wgs84_point');
 if(p.removedOn)return fail('removed_sign');
 if(typeof p.rvvCode!=='string'||!p.rvvCode.trim())return fail('missing_rvv_code');
 if(!Array.isArray(p.supplementarySigns))return fail('missing_supplementary_array');
 const groups=new Map(),issues=[];
 for(const s of p.supplementarySigns){
  if(!s||typeof s.signCode!=='string'||!Number.isInteger(s.index)||s.index<0)return fail('invalid_supplementary_schema');
  const row={code:s.signCode,text:s.text??null,opening_hours:s.openingHours??null};
  if(row.text!==null&&typeof row.text!=='string'||row.opening_hours!==null&&typeof row.opening_hours!=='string')return fail('invalid_supplementary_value');
  if(!groups.has(s.index))groups.set(s.index,[]);groups.get(s.index).push(row);
  if(s.signCode==='OTHER')issues.push({code:'unresolved_OTHER',index:s.index});
  if(['OB401','OB411','OB254','OB256','OB259','OTHER'].includes(s.signCode)&&!s.text?.trim()&&!s.openingHours?.trim())issues.push({code:'missing_value',index:s.index});
 }
 const bearing=typeof p.bearing==='number'&&Number.isInteger(p.bearing)&&p.bearing>=0&&p.bearing<360?p.bearing:null;
 if(bearing===null)issues.push({code:'missing_or_invalid_bearing'});
 if(p.placement!=='ALONG')issues.push({code:'unresolved_mounting'});
 return {id:feature.id,lat:c[1],lon:c[0],rvv_code:p.rvvCode,black_code:p.blackCode??null,zone_code:p.zoneCode??null,bearing_deg:bearing,placement:p.placement??null,road_section_id:p.roadSectionId??null,county_code:p.countyCode??null,owner_type:p.ownerType??null,owner_code:p.ownerCode??null,driving_direction:p.drivingDirection??null,conditions:p.conditions??null,source_modified_on:p.lastModifiedOn??null,validated_on:p.validatedOn??null,private_property:p.privateProperty??null,missing_road_section:p.missingRoadSection??null,supplementary_groups:[...groups].sort((a,b)=>a[0]-b[0]).map(([index,parts])=>({index,parts})),tile:tileKey(c[1],c[0]),issues,source_version:5};
}
export function georgePreview(record,legacyNormalize){
 if(record.reason)return record;
 const zones={BEGIN:'ZB',END:'ZE',REPEAT:'ZH',UNKNOWN:'ZO'};
 const feature={type:'Feature',id:record.id,geometry:{type:'Point',coordinates:[record.lon,record.lat]},properties:{rvvCode:record.rvv_code,blackCode:record.black_code,zoneCode:record.zone_code===null?'':zones[record.zone_code]??'UNSUPPORTED',status:'PLACED',placement:record.placement==='ALONG'?'L':'B',bearing:record.bearing_deg,textSigns:[]}};
 const base=legacyNormalize(feature);if(base.reason)return base;
 const supplementary=[];
 for(const group of record.supplementary_groups){
  // Same index means components combined on ONE physical plate, never separate plates.
  if(group.parts.length!==1)return {id:record.id,reason:'combined_supplementary_asset_missing',details:group};
  const part=group.parts[0];
  const key=part.code.toUpperCase().replace(/^OB0+(\d+)/,(_,n)=>'OB'+Number(n));
  const descriptor=assetRegistry()[key];
  if(!descriptor||descriptor.role!=='supplementary'||descriptor.requires_value||part.text?.trim()||part.opening_hours?.trim())return {id:record.id,reason:'structured_supplementary_asset_missing',details:group};
  supplementary.push(key);
 }
 if(supplementary.length>2)return {id:record.id,reason:'preview_mounting_limit'};
 return {...base,source_version:5,supplementary_groups:record.supplementary_groups,supplementary_assets:supplementary,conditions:record.conditions,source_modified_on:record.source_modified_on};
}
