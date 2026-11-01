"""Render the scan assembly as actual displaced geometry, including clay QA.

Blender --factory-startup -b -t 8 --python tools/render_cliff_assembly.py
"""
from pathlib import Path
import bpy
import numpy as np
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/cliff-assembly'
SPAN = 91.44


def look_at(obj, position, target):
    obj.location = position
    obj.rotation_euler = (Vector(target)-obj.location).to_track_quat('-Z', 'Y').to_euler()


def surface(name, folder, width, height, upright=True):
    field = np.flipud(np.load(folder / 'mesh_height.npy'))
    ny, nx = field.shape
    x, y = np.meshgrid(np.linspace(0, width, nx), np.linspace(0, height, ny))
    vertices = np.stack((x, -field, y), axis=-1) if upright else np.stack((x, y-height-1, field), axis=-1)
    vertices = vertices.astype(np.float32).reshape(-1, 3)
    a = (np.arange(ny-1)[:, None]*nx + np.arange(nx-1)).ravel()
    faces = np.stack((a, a+1, a+nx+1, a+nx), axis=1).astype(np.int32)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(vertices)); mesh.vertices.foreach_set('co', vertices.ravel())
    mesh.loops.add(faces.size); mesh.loops.foreach_set('vertex_index', faces.ravel())
    mesh.polygons.add(len(faces))
    mesh.polygons.foreach_set('loop_start', np.arange(len(faces), dtype=np.int32)*4)
    mesh.polygons.foreach_set('loop_total', np.full(len(faces), 4, np.int32))
    mesh.polygons.foreach_set('use_smooth', np.ones(len(faces), bool))
    uv = mesh.uv_layers.new(name='Scan projection')
    coordinates = np.stack((x/width, y/height), axis=-1).reshape(-1, 2)
    uv.data.foreach_set('uv', coordinates[faces.ravel()].astype(np.float32).ravel())
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    mat = bpy.data.materials.new(name + ' registered PBR')
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes.get('Principled BSDF')
    for filename, socket, data in (('basecolor.png', 'Base Color', False), ('roughness.png', 'Roughness', True)):
        node = nodes.new('ShaderNodeTexImage')
        node.image = bpy.data.images.load(str(folder / filename))
        node.image.colorspace_settings.name = 'Non-Color' if data else 'sRGB'
        links.new(node.outputs['Color'], bsdf.inputs[socket])
    tex = nodes.new('ShaderNodeTexImage')
    tex.image = bpy.data.images.load(str(folder / 'mesh_detail_normal.png'))
    tex.image.colorspace_settings.name = 'Non-Color'
    normal = nodes.new('ShaderNodeNormalMap')
    links.new(tex.outputs['Color'], normal.inputs['Color'])
    links.new(normal.outputs['Normal'], bsdf.inputs['Normal'])
    bsdf.inputs['Specular'].default_value = .25
    obj.data.materials.append(mat)
    return obj, mat


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    cliff, material = surface('Joined 300 ft coastal scan face', WORK, SPAN, SPAN)
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = 32
    scene.cycles.use_denoising = True
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = 8
    world = bpy.data.worlds.new('Soft sky')
    scene.world = world
    world.use_nodes = True
    world.node_tree.nodes['Background'].inputs['Color'].default_value = (.65, .76, 1, 1)
    world.node_tree.nodes['Background'].inputs['Strength'].default_value = .35
    sun = bpy.data.objects.new('Raking sunlight', bpy.data.lights.new('Raking sunlight', 'SUN'))
    bpy.context.collection.objects.link(sun)
    sun.data.energy = 2.5
    sun.data.angle = .08
    look_at(sun, (-50, -90, 110), (45, 0, 30))
    camera = bpy.data.objects.new('Review camera', bpy.data.cameras.new('Review camera'))
    bpy.context.collection.objects.link(camera)
    scene.camera = camera
    camera.data.clip_end = 1000
    scene.render.resolution_x, scene.render.resolution_y = 1600, 1400
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.view_settings.view_transform = 'Filmic'
    scene.view_settings.look = 'Medium High Contrast'
    camera.data.type = 'ORTHO'
    camera.data.ortho_scale = 108
    look_at(camera, (115, -170, 94), (45, 0, 43))
    scene.render.filepath = str(WORK / 'wide.png')
    bpy.ops.render.render(write_still=True)
    camera.data.type = 'PERSP'
    camera.data.lens = 44
    field = np.load(WORK / 'mesh_height.npy')
    depth = float(np.max(field))
    look_at(camera, (39, -depth-14, 12), (44, -depth/2, 13))
    scene.render.filepath = str(WORK / 'close.png')
    bpy.ops.render.render(write_still=True)
    bsdf = material.node_tree.nodes.get('Principled BSDF')
    base = bsdf.inputs['Base Color']
    link = base.links[0]
    source = link.from_socket
    material.node_tree.links.remove(link)
    base.default_value = (.35, .35, .35, 1)
    # Disable normal mapping too: this view exposes the actual silhouette/relief.
    normal_link = bsdf.inputs['Normal'].links[0]
    normal_source = normal_link.from_socket
    material.node_tree.links.remove(normal_link)
    scene.render.filepath = str(WORK / 'clay.png')
    bpy.ops.render.render(write_still=True)
    material.node_tree.links.new(source, base)
    material.node_tree.links.new(normal_source, bsdf.inputs['Normal'])
    bpy.ops.wm.save_as_mainfile(filepath=str(WORK / 'coastal_scan_assembly.blend'))


if __name__ == '__main__':
    main()
