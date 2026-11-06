// Offline data-only tile builder. Node 22.13+ (built-in SQLite); no third-party runtime packages.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {DatabaseSync} from 'node:sqlite';
import {readGeoJSON} from './george_geojson_stream.mjs';
import {normalizeGeorge,georgePreview} from './george_v5.mjs';
import {normalize} from './import_ndw_signs.mjs';
export async function buildGeorge(file,out,{statusFilter,sourceUrl=null,emitNormalized=true,onProgress=()=>{}}={}){
 if(statusFilter!=='PLACED')throw Error('Require explicit PLACED query provenance');
 if(fs.existsSync(out))throw Error('Output must be a new directory; old tiles must never survive a rebuild');
 fs.mkdirSync(out,{recursive:true});fs.mkdirSync(path.join(out,'render'));
 const dbFile=path.join(out,'.build.sqlite'),db=new DatabaseSync(dbFile);
 const source={provider:'NDW George public current-state v5',status_filter:statusFilter,url:sourceUrl,format:'WGS84 GeoJSON',schema_url:'https://wegkenmerken.ndw.nu/api/open-api/api-docs/v5'};
 const metadata=fs.existsSync(file+'.source.json')?JSON.parse(fs.readFileSync(file+'.source.json')):null;
 if(metadata){if(metadata.status_filter!==statusFilter||metadata.crs!=='CRS84')throw Error('Source provenance mismatch');source.url=sourceUrl||metadata.url;source.snapshot=metadata.snapshot;source.scope=metadata.scope;}
 const totals={render_tiles:0,render_bytes:0,input:0,valid:0,invalid:0,duplicate_ids:0,tiles:0,preview_eligible:0,subplates:0,unresolved_OTHER:0},rejects={};
 try{
 db.exec('PRAGMA journal_mode=OFF;PRAGMA synchronous=OFF;PRAGMA cache_size=-65536;PRAGMA temp_store=FILE;PRAGMA locking_mode=EXCLUSIVE');
 db.exec('CREATE TABLE records(id TEXT PRIMARY KEY,tile TEXT,body TEXT,preview TEXT,duplicate INTEGER DEFAULT 0)');
 const insert=db.prepare('INSERT INTO records(id,tile,body,preview) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET duplicate=1');
 db.exec('BEGIN');
 for await(const f of readGeoJSON(file,source)){
  totals.input++;const row=normalizeGeorge(f,{statusFilter});
  if(row.reason){totals.invalid++;rejects[row.reason]=(rejects[row.reason]||0)+1;continue;}
  const decision=georgePreview(row,normalize);
  const compact=decision.reason?{reason:decision.reason}:{id:decision.id,lat:decision.lat,lon:decision.lon,asset:decision.asset,bearing_deg:decision.bearing_deg,supplementary_assets:decision.supplementary_assets,source_version:5};
  const body=emitNormalized?row:{id:row.id,subplate_count:row.supplementary_groups.reduce((n,g)=>n+g.parts.length,0),unresolved_other:row.supplementary_groups.reduce((n,g)=>n+g.parts.filter(p=>p.code==='OTHER').length,0)};
  insert.run(row.id,row.tile,JSON.stringify(body),JSON.stringify(compact));
  if(totals.input%10000===0){db.exec('COMMIT;BEGIN');onProgress({phase:'ingest',records:totals.input});}
 }
 if(metadata&&metadata.sha256!==source.sha256)throw Error('Source checksum mismatch');
 db.exec('COMMIT');
 totals.duplicate_ids=db.prepare('SELECT count(*) AS n FROM records WHERE duplicate=1').get().n;
 // Duplicate IDs are quarantined, including conflicting copies. Never pick an arbitrary winner.
 db.exec('DELETE FROM records WHERE duplicate=1');
 const expectedRows=db.prepare('SELECT count(*) AS n FROM records').get().n;
 onProgress({phase:'index',records:totals.input});db.exec('CREATE INDEX by_tile ON records(tile,id)');
 const tileQuery=db.prepare('SELECT DISTINCT tile FROM records ORDER BY tile');
 const rowsQuery=db.prepare('SELECT body,preview FROM records WHERE tile=? ORDER BY id');
 const previewRecords=[];
 for(const {tile}of tileQuery.iterate()){
  const rows=[],render=[];let tileRows=0;
  for(const raw of rowsQuery.iterate(tile)){
   if(tileRows++>=20000)throw Error('Tile exceeds 20000 records; review source density before publishing');
   const row=JSON.parse(raw.body),preview=JSON.parse(raw.preview);if(emitNormalized)rows.push(row);totals.valid++;
   if(emitNormalized){for(const g of row.supplementary_groups)for(const p of g.parts){totals.subplates++;if(p.code==='OTHER')totals.unresolved_OTHER++;}}else{totals.subplates+=row.subplate_count;totals.unresolved_OTHER+=row.unresolved_other;}
   if(!preview.reason){totals.preview_eligible++;const item={id:preview.id,lat:preview.lat,lon:preview.lon,asset:preview.asset,bearing_deg:preview.bearing_deg,supplementary_assets:preview.supplementary_assets,source_version:5,mounting:preview.mounting};render.push(item);if(previewRecords.length<40)previewRecords.push(item);}
   else rejects[preview.reason]=(rejects[preview.reason]||0)+1;
  }
  if(emitNormalized)fs.writeFileSync(path.join(out,tile+'.json'),JSON.stringify({schema_version:1,kind:'george_v5_tile',key:tile,records:rows})+'\n');
  if(render.length){const bytes=JSON.stringify({schema_version:1,kind:'george_v5_render_tile',key:tile,records:render})+'\n';fs.writeFileSync(path.join(out,'render',tile+'.json'),bytes);totals.render_tiles++;totals.render_bytes+=Buffer.byteLength(bytes);}
  totals.tiles++;if(totals.tiles%1000===0)onProgress({phase:'tiles',tiles:totals.tiles,records:totals.valid});
 }
 if(totals.valid!==expectedRows)throw Error('Tile export count differs from validated source rows');
 const manifest={schema_version:1,kind:'george_v5_tile_store',zoom:17,file_pattern:emitNormalized?'g17_{x}_{y}.json':null,runtime_file_pattern:'render/g17_{x}_{y}.json',source,totals,issues:rejects,runtime_policy:{network:false,photos:false,ocr:false,national_scan:false},limitations:['Opt-in tile loader retains a 40-sign simultaneous preview cap; full-density production performance is not yet validated.','Existing preview still limited to 40 signs.','Missing source values and unsupported artwork stay unresolved.']};
 fs.writeFileSync(path.join(out,'preview.json'),JSON.stringify({schema_version:1,kind:'ndw_small_area_preview',source,records:previewRecords},null,2)+'\n');
 // Completion marker written last. Consumers must require it.
 fs.writeFileSync(path.join(out,'manifest.json'),JSON.stringify(manifest,null,2)+'\n');
 return manifest;
 }finally{db.close();fs.unlinkSync(dbFile);}
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 const[file,out,statusFilter,mode]=process.argv.slice(2);
 if(!file||!out)throw Error('Usage: node tools/build_george_tiles.mjs input.geojson[.gz] NEW-output-dir PLACED');
 const m=await buildGeorge(file,out,{statusFilter,emitNormalized:mode!=='--runtime-only',onProgress:p=>{if(p.phase==='tiles'||p.records%100000===0)console.log(JSON.stringify(p));}});console.log(JSON.stringify(m.totals));
}
