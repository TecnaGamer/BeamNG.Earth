// Stream a GeoJSON FeatureCollection without putting the country into memory.
import fs from 'node:fs';
import zlib from 'node:zlib';
import {createHash} from 'node:crypto';
export async function* readGeoJSON(file,source){
 const hash=createHash('sha256'),raw=fs.createReadStream(file);
 raw.on('data',b=>hash.update(b));
 const input=file.endsWith('.gz')?raw.pipe(zlib.createGunzip()):raw;
 if(input!==raw)raw.on('error',err=>input.destroy(err));
 input.setEncoding('utf8');
 let header='',prefix=null,tail='',object='',depth=0,quoted=false,escaped=false,ended=false,wantValue=true,hadValue=false;
 try{
 for await(const chunk of input){
  let data=chunk;
  if(prefix===null){header+=data;const match=/"features"\s*:\s*\[/.exec(header);
   if(!match){if(header.length>1024*1024)throw Error('Missing features array or oversized header');continue;}
   const end=match.index+match[0].length;prefix=header.slice(0,end-1);data=header.slice(end);header='';
  }
  for(const ch of data){
   if(ended){tail+=ch;if(tail.length>1024*1024)throw Error('Oversized GeoJSON trailer');continue;}
   if(depth===0){
    if(/\s/.test(ch))continue;
    if(ch===']'){if(wantValue&&hadValue)throw Error('Trailing comma');ended=true;continue;}
    if(ch===','&&!wantValue){wantValue=true;continue;}
    if(ch!=='{'||!wantValue)throw Error('Expected GeoJSON feature object');
    object='{';depth=1;quoted=false;escaped=false;continue;
   }
   object+=ch;if(object.length>1024*1024)throw Error('Feature exceeds 1 MB');
   if(quoted){if(escaped)escaped=false;else if(ch==='\\')escaped=true;else if(ch==='"')quoted=false;}
   else if(ch==='"')quoted=true;else if(ch==='{')depth++;else if(ch==='}')depth--;
   if(depth===0){yield JSON.parse(object);object='';wantValue=false;hadValue=true;}
  }
 }
 if(prefix===null||!ended||depth!==0)throw Error('Incomplete FeatureCollection');
 const wrapper=JSON.parse(prefix+'[]'+tail);
 if(wrapper.type!=='FeatureCollection')throw Error('Expected FeatureCollection');
 if(wrapper.crs&&!/CRS84|4326/.test(JSON.stringify(wrapper.crs)))throw Error('Only WGS84 input is supported');
 source.sha256=hash.digest('hex');
 }finally{raw.destroy();input.destroy();}
}
