// Indexed Collada exporter for this small test library. Positions remain shared across faces.
export function collada(name, positions, triangles, texture=null, uvs=null) {
 const normals=[], mats=[...new Set(triangles.map(t=>t[0]))], groups=Object.fromEntries(mats.map(m=>[m,[]]));
 for(const [mat,a,b,c] of triangles) {
  const A=positions[a],B=positions[b],C=positions[c],u=B.map((v,i)=>v-A[i]),v=C.map((x,i)=>x-A[i]);
  const nn=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]],l=Math.hypot(...nn);
  if(l<1e-12)throw Error('Degenerate triangle');const ni=normals.length;normals.push(nn.map(v=>v/l));
  groups[mat].push(...[a,b,c].flatMap(i=>[i,ni,i]));
 }
 const num=a=>a.flat().map(x=>Number(x.toFixed(8))).join(' ');
 const source=(id,a,params)=>`<source id="${id}"><float_array id="${id}_a" count="${a.length*params.length}">${num(a)}</float_array><technique_common><accessor source="#${id}_a" count="${a.length}" stride="${params.length}">${params.map(p=>`<param name="${p}" type="float"/>`).join('')}</accessor></technique_common></source>`;
 return `<?xml version="1.0" encoding="utf-8"?><COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1"><asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>${texture?`<library_images><image id="image"><init_from>${texture}</init_from></image></library_images>`:''}<library_effects>${mats.map(m=>`<effect id="${m}_fx"><profile_COMMON>${m==='bne_nl_test_metal'?'':'<newparam sid="surface"><surface type="2D"><init_from>image</init_from></surface></newparam><newparam sid="sampler"><sampler2D><source>surface</source></sampler2D></newparam>'}<technique sid="common"><lambert><diffuse>${m==='bne_nl_test_metal'?'<color>0.56 0.58 0.60 1</color>':'<texture texture="sampler" texcoord="UVMap"/>'}</diffuse></lambert></technique></profile_COMMON></effect>`).join('')}</library_effects><library_materials>${mats.map(m=>`<material id="${m}" name="${m}"><instance_effect url="#${m}_fx"/></material>`).join('')}</library_materials><library_geometries><geometry id="mesh" name="${name}"><mesh>${source('pos',positions,['X','Y','Z'])}${source('normal',normals,['X','Y','Z'])}${source('uv',uvs||positions.map(()=>[0,0]),['S','T'])}<vertices id="verts"><input semantic="POSITION" source="#pos"/></vertices>${mats.map(m=>`<triangles material="${m}_symbol" count="${groups[m].length/9}"><input semantic="VERTEX" source="#verts" offset="0"/><input semantic="NORMAL" source="#normal" offset="1"/><input semantic="TEXCOORD" source="#uv" offset="2" set="0"/><p>${groups[m].join(' ')}</p></triangles>`).join('')}</mesh></geometry></library_geometries><library_visual_scenes><visual_scene id="Scene"><node id="${name}" name="${name}"><instance_geometry url="#mesh"><bind_material><technique_common>${mats.map(m=>`<instance_material symbol="${m}_symbol" target="#${m}"><bind_vertex_input semantic="UVMap" input_semantic="TEXCOORD" input_set="0"/></instance_material>`).join('')}</technique_common></bind_material></instance_geometry></node></visual_scene></library_visual_scenes><scene><instance_visual_scene url="#Scene"/></scene></COLLADA>\n`;
}

export function plate(outline,width,height,frontMaterial) {
 let points=outline.map(([u,v])=>[(u-.5)*width,(.5-v)*height]);
 const area=points.reduce((s,p,i)=>{const q=points[(i+1)%points.length];return s+p[0]*q[1]-q[0]*p[1];},0);
 if(area<0)points.reverse();
 // Exact parallel 3 mm inward offset of a convex outline, rather than a scaled fake rim.
 const edge=(a,b)=>{const x=b[0]-a[0],y=b[1]-a[1],l=Math.hypot(x,y);return [-y/l,x/l];};
 const inset=points.map((p,i)=>{
  const a=edge(points[(i+points.length-1)%points.length],p),b=edge(p,points[(i+1)%points.length]);
  const det=a[0]*b[1]-a[1]*b[0];
  if(Math.abs(det)<1e-9)return [p[0]+a[0]*.003,p[1]+a[1]*.003];
  return [p[0]+.003*(b[1]-a[1])/det,p[1]+.003*(a[0]-b[0])/det];
 });
 const positions=[[0,-.048,0],[0,-.045,0]],uvs=[[.5,.5],[.5,.5]],rings=[];
 for(const [pts,y] of [[points,-.048],[points,-.033],[inset,-.033],[inset,-.045]]) {
  rings.push(pts.map(([x,z])=>{const i=positions.length;positions.push([x,y,z]);uvs.push([x/width+.5,z/height+.5]);return i;}));
 }
 const triangles=[],n=points.length,metal='bne_nl_test_metal';
 for(let i=0;i<n;i++) {const j=(i+1)%n;triangles.push([frontMaterial,0,rings[0][i],rings[0][j]],[metal,1,rings[3][j],rings[3][i]]);
  for(let r=0;r<3;r++)triangles.push([metal,rings[r][i],rings[r+1][i],rings[r+1][j]],[metal,rings[r][i],rings[r+1][j],rings[r][j]]);
 }
 return {positions,triangles,uvs};
}
