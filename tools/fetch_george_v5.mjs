// Explicit offline download/cache command. Never called by BeamNG.
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
export async function fetchGeorge(cache,bbox,{fetchImpl=fetch}={}){
 if(!Array.isArray(bbox)||bbox.length!==4||!bbox.every(Number.isFinite)||bbox[0]>=bbox[2]||bbox[1]>=bbox[3]||bbox[2]-bbox[0]>.02||bbox[3]-bbox[1]>.02)throw Error('Use an explicit small WGS84 pilot bbox');
 const url=new URL('https://wegkenmerken.ndw.nu/api/rest/static-road-data/traffic-signs/v5/current-state');
 url.searchParams.set('bbox',bbox.join(','));url.searchParams.set('status','PLACED');url.searchParams.set('crs','http://www.opengis.net/def/crs/OGC/1.3/CRS84');
 const key=createHash('sha256').update(url.href).digest('hex'),file=path.join(cache,key+'.geojson'),meta=file+'.source.json';
 fs.mkdirSync(cache,{recursive:true});
 if(fs.existsSync(file)&&fs.existsSync(meta)){
  const m=JSON.parse(fs.readFileSync(meta)),sha=createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  if(m.url!==url.href||m.sha256!==sha)throw Error('Cache provenance/hash mismatch');
  return {file,meta,cached:true};
 }
 if(fs.existsSync(file)||fs.existsSync(meta))throw Error('Incomplete cache entry; review it before retrying');
 const response=await fetchImpl(url,{headers:{Accept:'application/geo+json'},signal:AbortSignal.timeout(60000)});
 if(!response.ok)throw Error('George download failed: HTTP '+response.status);
 const chunks=[];let bytes=0;
 for await(const chunk of response.body){bytes+=chunk.length;if(bytes>50*1024*1024)throw Error('Pilot response exceeds 50 MB');chunks.push(chunk);}
 const buffer=Buffer.concat(chunks),json=JSON.parse(buffer);
 if(json.type!=='FeatureCollection'||!Array.isArray(json.features))throw Error('Expected v5 FeatureCollection');
 if(json.features.some(f=>!Array.isArray(f.properties?.supplementarySigns)))throw Error('Response does not match George v5');
 fs.writeFileSync(file,buffer,{flag:'wx'});
 fs.writeFileSync(meta,JSON.stringify({provider:'NDW George v5',url:url.href,status_filter:'PLACED',crs:'CRS84',sha256:createHash('sha256').update(buffer).digest('hex')},null,2)+'\n',{flag:'wx'});
 return {file,meta,cached:false};
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 const[cache,box]=process.argv.slice(2);if(!cache||!box)throw Error('Usage: node tools/fetch_george_v5.mjs cache-directory west,south,east,north');
 console.log(JSON.stringify(await fetchGeorge(cache,box.split(',').map(Number))));
}
