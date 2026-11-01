"""Project real scan geometry and its registered material into a wall frame.

Run in Blender: --factory-startup -b -t 8 --python tools/project_cliff_scans.py
  -- --asset coastal_cliff_02 --pixels-per-metre 90
The camera sees along +Y; output X/Y/Z normals mean world X/Z/-Y. Depth is
measured from mesh positions, never estimated from the diffuse image.
"""
import argparse
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/cliff-scans'


def aov(material, name, socket, scalar=False):
    node = material.node_tree.nodes.new('ShaderNodeOutputAOV')
    node.name = name
    material.node_tree.links.new(socket, node.inputs['Value' if scalar else 'Color'])


def scalar(nodes, links, operation, value, socket):
    node = nodes.new('ShaderNodeMath')
    node.operation = operation
    node.inputs[1].default_value = value
    links.new(socket, node.inputs[0])
    return node.outputs[0]


def projection_normal(nodes, links, source):
    split = nodes.new('ShaderNodeSeparateXYZ')
    links.new(source, split.inputs[0])
    combine = nodes.new('ShaderNodeCombineXYZ')
    links.new(split.outputs['X'], combine.inputs['X'])
    links.new(split.outputs['Z'], combine.inputs['Y'])
    links.new(scalar(nodes, links, 'MULTIPLY', -1, split.outputs['Y']), combine.inputs['Z'])
    encode = nodes.new('ShaderNodeVectorMath')
    encode.operation = 'MULTIPLY_ADD'
    encode.inputs[1].default_value = (.5, .5, .5)
    encode.inputs[2].default_value = (.5, .5, .5)
    links.new(combine.outputs[0], encode.inputs[0])
    return encode.outputs[0]


def prepare_material(material):
    nodes, links = material.node_tree.nodes, material.node_tree.links
    bsdf = next(n for n in nodes if n.type == 'BSDF_PRINCIPLED')
    geometry = nodes.new('ShaderNodeNewGeometry')
    position = nodes.new('ShaderNodeSeparateXYZ')
    links.new(geometry.outputs['Position'], position.inputs[0])
    depth = scalar(nodes, links, 'MULTIPLY', -1, position.outputs['Y'])
    aov(material, 'depth', depth, True)
    aov(material, 'geonormal', projection_normal(nodes, links, geometry.outputs['Normal']))
    shaded_normal = bsdf.inputs['Normal'].links[0].from_socket if bsdf.inputs['Normal'].is_linked else geometry.outputs['Normal']
    aov(material, 'normal', projection_normal(nodes, links, shaded_normal))
    color = bsdf.inputs['Base Color'].links[0].from_socket
    aov(material, 'albedo', color)
    rough = bsdf.inputs['Roughness'].links[0].from_socket
    aov(material, 'roughness', rough, True)
    packed = next((n for n in nodes if n.type == 'TEX_IMAGE' and '_arm_' in n.image.filepath), None)
    if packed:
        channels = nodes.new('ShaderNodeSeparateRGB')
        links.new(packed.outputs['Color'], channels.inputs[0])
        aov(material, 'ao', channels.outputs['R'], True)
    else:
        white = nodes.new('ShaderNodeValue')
        white.outputs[0].default_value = 1
        aov(material, 'ao', white.outputs[0], True)
    # Unlit projection preserves the captured, de-lit base color.
    emission = nodes.new('ShaderNodeEmission')
    links.new(color, emission.inputs['Color'])
    output = next(n for n in nodes if n.type == 'OUTPUT_MATERIAL')
    links.new(emission.outputs[0], output.inputs['Surface'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset', required=True)
    parser.add_argument('--pixels-per-metre', type=float, default=90)
    options = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    folder = WORK / options.asset
    output = folder / 'projection'
    output.mkdir(exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    source = folder / f'{options.asset}_8k.gltf'
    bpy.ops.import_scene.gltf(filepath=str(source))
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    corners = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    lo = Vector(tuple(min(p[i] for p in corners) for i in range(3)))
    hi = Vector(tuple(max(p[i] for p in corners) for i in range(3)))
    width, height = hi.x - lo.x, hi.z - lo.z
    centre = (lo + hi) / 2
    camera = bpy.data.objects.new('Orthographic scan projector', bpy.data.cameras.new('Projector'))
    bpy.context.collection.objects.link(camera)
    camera.location = (centre.x, lo.y - 100, centre.z)
    camera.rotation_euler = (Vector((centre.x, centre.y, centre.z)) - camera.location).to_track_quat('-Z', 'Y').to_euler()
    camera.data.type = 'ORTHO'
    camera.data.ortho_scale = width
    camera.data.clip_end = 1000
    scene = bpy.context.scene
    scene.camera = camera
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = 4
    scene.cycles.use_denoising = False
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = 8
    scene.render.resolution_x = min(8192, round(width * options.pixels_per_metre))
    scene.render.resolution_y = round(scene.render.resolution_x * height / width)
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    channels = (('albedo', False), ('normal', False), ('geonormal', False),
                ('depth', True), ('roughness', True), ('ao', True))
    for name, is_scalar in channels:
        layer = bpy.context.view_layer.aovs.add()
        layer.name = name
        layer.type = 'VALUE' if is_scalar else 'COLOR'
    for material in {m for o in meshes for m in o.data.materials if m}:
        prepare_material(material)
    scene.use_nodes = True
    nodes, links = scene.node_tree.nodes, scene.node_tree.links
    nodes.clear()
    render = nodes.new('CompositorNodeRLayers')
    for name, is_scalar in (*channels, ('Alpha', True)):
        node = nodes.new('CompositorNodeOutputFile')
        node.base_path = str(output)
        node.file_slots[0].path = name + '_'
        node.format.file_format = 'OPEN_EXR'
        node.format.color_mode = 'RGB'
        node.format.color_depth = '32' if name == 'depth' else '16'
        links.new(render.outputs[name], node.inputs[0])
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.filepath = str(output / 'source.png')
    (output / 'projection.json').write_text(json.dumps({
        'asset': options.asset, 'width_m': width, 'height_m': height,
        'pixels': [scene.render.resolution_x, scene.render.resolution_y],
        'bounds_min': list(lo), 'bounds_max': list(hi),
        'depth_definition': '-world_y', 'normal_axes': ['world_x', 'world_z', '-world_y'],
        'geometry_triangles': sum(len(o.data.polygons) for o in meshes),
    }, indent=2))
    bpy.ops.render.render(write_still=True)


if __name__ == '__main__':
    main()
