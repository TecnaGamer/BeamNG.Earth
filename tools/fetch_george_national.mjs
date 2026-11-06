// Full-country current-state v5 cache. No input array or response body is buffered in memory.
import fs from 'node:fs';import path from 'node:path';import zlib from 'node:zlib';import{Readable,Transform}from'node:stream';import{pipeline}from'node:stream/promises';import{createHash}from'node:crypto';import{fileURLToPath}from'node:url';
export async function fetchNational(cache,{snapshot,fetchImpl=fetch,onProgress=()=>{}}={}){
 if(!/^\d{4}-\d{2}-\d{2}$/.test(snapshot))throw Error('Use an explicit snapshot date YYYY-MM-DD');
 fs.mkdirSync(cache,{recursive:true});const url='https://wegkenmerken.ndw.nu/api/rest/static-road-data/traffic-signs/v5/current-state?status=PLACED&crs=http%3A%2F%2Fwww.opengis.net%2Fdef%2Fcrs%2FOGC%2F1.3%2FCRS84',file=path.join(cache,'netherlands-v5-'+snapshot+'.geojson.gz'),meta=file+'.source.json';
 if(fs.existsSync(file)&&fs.existsSync(meta)){const m=JSON.parse(fs.readFileSync(meta));if(m.url!==url||m.status_filter!=='PLACED'||m.crs!=='CRS84')throw Error('Cache provenance mismatch');const h=createHash('sha256');for await(const b of fs.createReadStream(file))h.update(b);if(h.digest('hex')!==m.sha256)throw Error('Cache checksum mismatch');return{file,meta,cached:true};}
 if(fs.existsSync(file)||fs.existsSync(meta)||fs.existsSync(file+'.part'))throw Error('Incomplete cache exists; review it before retrying');
 const response=await fetchImpl(url,{headers:{Accept:'application/geo+json','User-Agent':'BeamNG.Earth-GeorgeImport/1.0'},signal:AbortSignal.timeout(1800000)});
 if(!response.ok)throw Error('George national export HTTP '+response.status);
 let bytes=0,last=0,head='';const hash=createHash('sha256');
 const inspect=new Transform({transform(chunk,enc,cb){bytes+=chunk.length;if(head.length<20000)head+=chunk.toString('utf8').slice(0,20000-head.length);if(bytes-last>=32*1024*1024){last=bytes;onProgress(bytes);}cb(null,chunk);}});
 const digest=new Transform({transform(chunk,enc,cb){hash.update(chunk);cb(null,chunk);}});
 await pipeline(Readable.fromWeb(response.body),inspect,zlib.createGzip({level:3}),digest,fs.createWriteStream(file+'.part',{flags:'wx'}));
 if(!head.includes('supplementarySigns')||!head.includes('FeatureCollection'))throw Error('Response does not match George v5; partial cache retained');
 for(let attempt=0;;attempt++){try{await fs.promises.rename(file+'.part',file);break;}catch(e){if(!['EPERM','EBUSY','EACCES'].includes(e.code)||attempt>=5)throw e;await new Promise(r=>setTimeout(r,50*(attempt+1)));}}fs.writeFileSync(meta,JSON.stringify({provider:'NDW George v5',scope:'Netherlands, all owners',url,status_filter:'PLACED',crs:'CRS84',snapshot,etag:response.headers.get('etag'),decoded_bytes:bytes,compressed_bytes:fs.statSync(file).size,sha256:hash.digest('hex')},null,2)+'\n',{flag:'wx'});
 return{file,meta,cached:false,bytes};
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 const[cache,date,confirmation]=process.argv.slice(2);if(!cache||confirmation!=='--netherlands')throw Error('Usage: node tools/fetch_george_national.mjs cache-dir YYYY-MM-DD --netherlands');
 console.log(JSON.stringify(await fetchNational(cache,{snapshot:date,onProgress:n=>console.log('Received',Math.round(n/1024/1024),'MiB of decoded v5 data')})));
}
