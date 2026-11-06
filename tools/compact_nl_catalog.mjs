// Offline lossless repack. No image resizing, palette reduction or model changes.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import {fileURLToPath}from'node:url';
import {inflateSync,deflateSync}from'node:zlib';
import {writeMaterialBanks}from'./nl_material_banks.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const base=path.join(root,'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth');
const dir=path.join(base,'nl_catalog');
const crcTable=Array.from({length:256},(_,n)=>{for(let k=0;k<8;k++)n=n&1?0xedb88320^(n>>>1):n>>>1;return n>>>0;});
function chunk(type,data){
  const out=Buffer.alloc(data.length+12);out.writeUInt32BE(data.length);out.write(type,4);data.copy(out,8);
  let crc=0xffffffff;for(const b of out.subarray(4,-4))crc=crcTable[(crc^b)&255]^(crc>>>8);
  out.writeUInt32BE((crc^0xffffffff)>>>0,out.length-4);return out;
}
let before=0,after=0,count=0;
for(const name of fs.readdirSync(dir).filter(n=>n.endsWith('.png'))){
  const file=path.join(dir,name),original=fs.readFileSync(file),chunks=[],idat=[];
  for(let i=8;i<original.length;){const n=original.readUInt32BE(i),type=original.toString('ascii',i+4,i+8);chunks.push({type,bytes:original.subarray(i,i+n+12)});if(type==='IDAT')idat.push(original.subarray(i+8,i+n+8));i+=n+12;}
  const pixels=inflateSync(Buffer.concat(idat)),compressed=deflateSync(pixels,{level:9,strategy:0});
  assert.deepEqual(inflateSync(compressed),pixels,'PNG scanlines must remain byte-identical');
  let inserted=false;const output=[original.subarray(0,8)];
  for(const c of chunks){if(c.type!=='IDAT')output.push(c.bytes);else if(!inserted){output.push(chunk('IDAT',compressed));inserted=true;}}
  const packed=Buffer.concat(output);before+=original.length;
  if(packed.length<original.length){fs.writeFileSync(file,packed);after+=packed.length;}else after+=original.length;
  count++;
}
const manifestFile=path.join(dir,'manifest.json'),manifest=JSON.parse(fs.readFileSync(manifestFile));
const materials=JSON.parse(fs.readFileSync(path.join(dir,'main.materials.json')));
const files=writeMaterialBanks(dir,materials,manifest.entries);
fs.writeFileSync(manifestFile,JSON.stringify(manifest,null,2)+'\n');
const runtimeFile=path.join(base,'nl_testset/manifest.json'),runtime=JSON.parse(fs.readFileSync(runtimeFile));
for(const sign of Object.values(runtime.signs))if(sign.catalog_source){
  assert(files.has(sign.material));sign.material_file='/levels/beamng_earth/art/shapes/beamngEarth/nl_catalog/'+files.get(sign.material);
}
fs.writeFileSync(runtimeFile,JSON.stringify(runtime,null,2)+'\n');
console.log(JSON.stringify({textures:count,original_bytes:before,packed_bytes:after,lossless_scanlines:true,material_banks:new Set(files.values()).size}));
