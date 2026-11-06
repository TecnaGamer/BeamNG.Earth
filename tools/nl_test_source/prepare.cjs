// One-time conversion of cached authentic SVGs, not procedural artwork.
// npm install --prefix tools/nl_test_source; node tools/nl_test_source/prepare.cjs
const fs=require('node:fs'),path=require('node:path'),{createRequire}=require('node:module');
const req=createRequire(path.resolve(process.argv[2]||__dirname,'package.json'));
const {Resvg}=req('@resvg/resvg-js'),{PNG}=req('pngjs');
function hull(p) {
 p.sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
 const cross=(a,b,c)=>(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);
 const side=a=>{const out=[];for(const p of a){while(out.length>1&&cross(out.at(-2),out.at(-1),p)<=0)out.pop();out.push(p);}return out.slice(0,-1);};
 return [...side(p),...side([...p].reverse())];
}
for(const code of (process.argv.slice(3).length?process.argv.slice(3):['A1-50','B6','C1','J16','A1-30-ZB','A2-30-ZE','OB504'])) {
 const svg=fs.readFileSync(path.join(__dirname,code+'.svg'));
 const renderer=new Resvg(svg,{fitTo:{mode:'width',value:1024}}), raw=PNG.sync.read(renderer.render().asPng());
 const points=[];
 for(let y=0;y<raw.height;y++) {let lo=-1,hi=-1;for(let x=0;x<raw.width;x++){if(raw.data[(y*raw.width+x)*4+3]>=128){if(lo<0)lo=x;hi=x;}} if(lo>=0)points.push([lo,y],[hi,y]);}
 const contour=hull(points).map(([x,y])=>[x/(raw.width-1),y/(raw.height-1)]);
 const rgb=new PNG({width:1024,height:1024,colorType:2});
 for(let y=0;y<1024;y++)for(let x=0;x<1024;x++) {
   const sy=Math.min(raw.height-1,Math.floor(y*raw.height/1024)),i=(sy*raw.width+x)*4,j=(y*1024+x)*4,a=raw.data[i+3]/255;
   for(let c=0;c<3;c++)rgb.data[j+c]=Math.round(raw.data[i+c]*a+[247,251,245][c]*(1-a));rgb.data[j+3]=255;
 }
 fs.writeFileSync(path.join(__dirname,code+'.color.png'),PNG.sync.write(rgb,{colorType:2,inputColorType:6,bitDepth:8}));
 fs.writeFileSync(path.join(__dirname,code+'.outline.json'),JSON.stringify({aspect:raw.height/raw.width,contour})+'\n');
 console.log(code,raw.width,raw.height,'contour vertices',contour.length);
}
