// Small lazy-load banks keep the catalogue complete without one JSON per model.
import fs from 'node:fs';
import path from 'node:path';
export function writeMaterialBanks(directory, materials, entries) {
  const names=Object.keys(materials).filter(n=>n!=='bne_nl_test_metal').sort();
  const files=new Map();
  for(let i=0;i<names.length;i+=32){
    const file=`material_bank_${String(i/32).padStart(3,'0')}.materials.json`;
    const bank={};
    for(const name of names.slice(i,i+32)){bank[name]=materials[name];files.set(name,file);}
    fs.writeFileSync(path.join(directory,file),JSON.stringify(bank,null,2)+'\n');
  }
  for(const entry of entries)for(const part of entry.parts){
    if(!files.has(part.material))throw Error('Missing catalogue material: '+part.material);
    part.material_file=files.get(part.material);
  }
  for(const name of fs.readdirSync(directory)){
    if(/^cat_[0-9a-f]{16}\.materials\.json$/.test(name))fs.unlinkSync(path.join(directory,name));
  }
  return files;
}
