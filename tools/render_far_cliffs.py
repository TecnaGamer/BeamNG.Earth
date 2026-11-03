"""Blender offline material/coverage preview; ground colours are illustrative.
Run after preview_far_cliffs.py, with Blender --background --python this_file.
"""
from pathlib import Path
import bpy, numpy as np
from mathutils import Vector
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'build/grand-canyon-cliffs'
d=np.load(OUT/'terrain.npz')
p,t,labels=d['positions'],d['tris'],d['labels']
v=p[t];cross=np.cross(v[:,1]-v[:,0],v[:,2]-v[:,0])
keep=cross[:,2]>1e-5
t,labels=t[keep],labels[keep]  # omit buried stitching skirts in this isolated preview
# If the separate chart is absent use the local geographic approximation.
chart=d['chart'] if 'chart' in d else np.column_stack((p[:,0]/.807,p[:,1],p[:,2]))/91.44
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
mesh=bpy.data.meshes.new('Grand Canyon real DEM')
mesh.from_pydata(p.tolist(),[],t.tolist());mesh.update()
obj=bpy.data.objects.new('Distant terrain - no extra cliff geometry',mesh)
bpy.context.collection.objects.link(obj)
mesh.polygons.foreach_set('use_smooth',np.ones(len(t),bool))
# Neutral illustrative terrain; real game uses aerial imagery here.
base=bpy.data.materials.new('Illustrative ground colours (not aerial imagery)');base.use_nodes=True
nodes=base.node_tree.nodes;links=base.node_tree.links
bs=nodes.get('Principled BSDF');bs.inputs['Roughness'].default_value=.95
attr=nodes.new('ShaderNodeAttribute');attr.attribute_name='ground'
links.new(attr.outputs['Color'],bs.inputs['Base Color'])
colors=mesh.color_attributes.new(name='ground',type='FLOAT_COLOR',domain='POINT')
z=np.clip((p[:,2]-700)/1800,0,1)
low=np.array([.29,.16,.10]);high=np.array([.43,.39,.28])
color=low[None,:]*(1-z[:,None])+high[None,:]*z[:,None]
colors.data.foreach_set('color',np.column_stack((color,np.ones(len(p)))).ravel())
rock=bpy.data.materials.new('Canyon generated albedo');rock.use_nodes=True
nodes=rock.node_tree.nodes;links=rock.node_tree.links
bs=nodes.get('Principled BSDF');bs.inputs['Roughness'].default_value=.94
tex=nodes.new('ShaderNodeTexImage');tex.image=bpy.data.images.load(str(ROOT/'mod/beamng_earth/levels/beamng_earth/art/shapes/beamngEarth/cliff/canyon_v3_base.color.png'))
tex.extension='REPEAT';links.new(tex.outputs['Color'],bs.inputs['Base Color'])
mesh.materials.append(base);mesh.materials.append(rock)
mesh.polygons.foreach_set('material_index',(labels>=0).astype(np.int32))
v=p[t];normal=np.cross(v[:,1]-v[:,0],v[:,2]-v[:,0])
axis=np.abs(normal[:,0])>=np.abs(normal[:,1])
uv=np.stack((np.where(axis[:,None],chart[t,1],chart[t,0]),chart[t,2]/2),-1)
uv-=np.floor(uv[:,0:1,:])
layer=mesh.uv_layers.new(name='Geographic cliff UV')
layer.data.foreach_set('uv',uv.reshape(-1))
bpy.ops.object.light_add(type='SUN',location=(0,-10000,18000));sun=bpy.context.object
sun.data.energy=2.0;sun.rotation_euler=(.48,-.6,-.3);sun.data.angle=.14
bpy.ops.object.camera_add(location=(1000,-13000,4800));cam=bpy.context.object
cam.rotation_euler=(Vector((0,1000,1300))-cam.location).to_track_quat('-Z','Y').to_euler()
cam.data.lens=29;cam.data.clip_end=150000
scene=bpy.context.scene;scene.camera=cam;scene.render.engine='BLENDER_EEVEE'
scene.eevee.use_gtao=True;scene.eevee.gtao_distance=50
scene.eevee.taa_render_samples=64
scene.world.color=(.25,.30,.38)
scene.render.resolution_x=1600;scene.render.resolution_y=1000;scene.render.resolution_percentage=100
scene.view_settings.view_transform='Standard';scene.view_settings.look='Medium High Contrast';scene.view_settings.exposure=0;scene.view_settings.gamma=1
scene.render.image_settings.file_format='PNG';scene.render.filepath=str(OUT/'canyon-material-preview.png')
bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'canyon-preview.blend'))
bpy.ops.render.render(write_still=True)
