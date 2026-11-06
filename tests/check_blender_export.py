"""Builds an animated, rigged, lit and PBR-shaded scene in Blender, exports it
to .ma, checks the file, then imports it back and compares the result.

Needs Blender, so unittest discovery skips it. Run:

    blender -b --factory-startup --python tests/check_blender_export.py
"""
import math
import os
import struct
import sys
import tempfile
import zlib

import bpy
from mathutils import Matrix, Vector

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import maya_scene_importer                                  # noqa: E402
from maya_scene_importer import ma_parser, ma_model, scene_builder  # noqa: E402

FAILS = []
EXPORT_FRAME = int(os.environ.get("EXPORT_FRAME", "1"))   # playhead at export


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        FAILS.append(label)
    return ok


def png(path):
    raw = b"\x00" + b"\x80\x80\x80" * 2
    chunk = lambda tag, data: (struct.pack(">I", len(data)) + tag + data
                               + struct.pack(">I", zlib.crc32(tag + data)))
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n"
                 + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw * 2))
                 + chunk(b"IEND", b""))


def close(a, b, tol=1e-3):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


folder = tempfile.mkdtemp(prefix="mayaio_export_")
tex = {}
for slot in ("albedo", "packed", "normal"):
    tex[slot] = os.path.join(folder, "crate_%s.png" % slot)
    png(tex[slot])

bpy.ops.wm.read_homefile(use_empty=True)
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 10
scene.render.fps = 25


# -- PBR material -------------------------------------------------------------
mat = bpy.data.materials.new("Crate")
nt = mat.node_tree
bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
bsdf.inputs['Metallic'].default_value = 0.7
bsdf.inputs['Alpha'].default_value = 0.5
bsdf.inputs['Emission Color'].default_value = (1, 0.5, 0, 1)
bsdf.inputs['Emission Strength'].default_value = 2.0


def image_node(path, non_color):
    n = nt.nodes.new('ShaderNodeTexImage')
    n.image = bpy.data.images.load(path)
    if non_color:
        n.image.colorspace_settings.name = 'Non-Color'
    return n


nt.links.new(image_node(tex["albedo"], False).outputs['Color'],
             bsdf.inputs['Base Color'])
split = nt.nodes.new('ShaderNodeSeparateColor')
nt.links.new(image_node(tex["packed"], True).outputs['Color'],
             split.inputs[0])
nt.links.new(split.outputs[1], bsdf.inputs['Roughness'])     # glTF: G = rough
nmap = nt.nodes.new('ShaderNodeNormalMap')
nt.links.new(image_node(tex["normal"], True).outputs['Color'],
             nmap.inputs['Color'])
nt.links.new(nmap.outputs['Normal'], bsdf.inputs['Normal'])


# -- animated box with an animated shape key -----------------------------------
bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0))
box = bpy.context.object
box.name = "Box"
box.data.materials.append(mat)
box.shape_key_add(name="Basis")
smile = box.shape_key_add(name="Smile")
for i, v in enumerate(smile.data):
    v.co.z += 0.25 if v.co.z > 0 else 0
for f, loc, rz, val in ((1, (0, 0, 0), 0, 0.0), (10, (2, 1, 0.5), 90, 1.0)):
    box.location = loc
    box.rotation_euler = (0, 0, math.radians(rz))
    box.keyframe_insert("location", frame=f)
    box.keyframe_insert("rotation_euler", frame=f)
    smile.value = val
    smile.keyframe_insert("value", frame=f)


# -- skinned arm on an animated armature ---------------------------------------
arm_data = bpy.data.armatures.new("Rig")
rig = bpy.data.objects.new("Rig", arm_data)
scene.collection.objects.link(rig)
rig.location = (-3, 0, 0)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode='EDIT')
prev = None
for i in range(3):
    eb = arm_data.edit_bones.new("b%d" % i)
    eb.head, eb.tail = (0, 0, i), (0, 0, i + 1)
    if prev:
        eb.parent, eb.use_connect = prev, True
    prev = eb
bpy.ops.object.mode_set(mode='OBJECT')

verts, faces = [], []
for i in range(7):                       # a strip of quads up the bones
    z = i * 0.5
    verts += [(-0.2, 0, z), (0.2, 0, z)]
    if i:
        a = 2 * (i - 1)
        faces.append((a, a + 1, a + 3, a + 2))
me = bpy.data.meshes.new("ArmMesh")
me.from_pydata(verts, [], faces)
skinned = bpy.data.objects.new("Arm", me)
scene.collection.objects.link(skinned)
skinned.parent = rig
skinned.matrix_parent_inverse = rig.matrix_world.inverted()
groups = [skinned.vertex_groups.new(name="b%d" % i) for i in range(3)]
for vi, (_, _, z) in enumerate(verts):
    groups[min(int(z), 2)].add([vi], 1.0, 'REPLACE')
skinned.modifiers.new("Armature", 'ARMATURE').object = rig
# two materials split per face, like a helmet and its visor
skinned.data.materials.append(mat)
skinned.data.materials.append(bpy.data.materials.new("Visor"))
for p in skinned.data.polygons:
    p.material_index = 1 if p.index >= 4 else 0

# an object that scales in from zero cannot be inverted on frame 1
bpy.ops.mesh.primitive_cube_add(size=1, location=(5, 0, 0))
pop = bpy.context.object
pop.name = "Pop"
for f, sc in ((1, 0.0), (10, 1.0)):
    pop.scale = (sc, sc, sc)
    pop.keyframe_insert("scale", frame=f)

pb = rig.pose.bones["b1"]
pb.rotation_mode = 'XYZ'
for f, ang, x in ((1, 0, -3), (10, 60, -1)):
    pb.rotation_euler = (math.radians(ang), 0, 0)
    pb.keyframe_insert("rotation_euler", frame=f)
    rig.location.x = x
    rig.keyframe_insert("location", frame=f)


# -- camera and lights ---------------------------------------------------------
cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
scene.collection.objects.link(cam)
cam.location = (0, -8, 2)
cam.rotation_euler = (math.radians(80), 0, 0)
for f, lens, y in ((1, 50, -8), (10, 85, -6)):
    cam.data.lens = lens
    cam.data.keyframe_insert("lens", frame=f)
    cam.location.y = y
    cam.keyframe_insert("location", frame=f)
spot = bpy.data.objects.new("Spot", bpy.data.lights.new("Spot", 'SPOT'))
scene.collection.objects.link(spot)
spot.location = (3, -3, 4)
spot.rotation_euler = (math.radians(45), 0, math.radians(45))
spot.data.energy = 500
spot.data.spot_size = math.radians(45)
spot.data.color = (1, 0.9, 0.8)
sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", 'SUN'))
scene.collection.objects.link(sun)
sun.data.energy = 3


# -- reference state, sampled before export ------------------------------------
def world_verts(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    m = ev.to_mesh()
    out = sorted(tuple(round(c, 3) for c in obj.matrix_world @ v.co)
                 for v in m.vertices)
    ev.to_mesh_clear()
    return out


def basis_world(obj):
    """World positions of the basis vertices: the placement alone, since
    shape key weight animation is not imported back."""
    return sorted(tuple(round(c, 3) for c in obj.matrix_world @ v.co)
                  for v in obj.data.vertices)


FRAMES = (1, 5, 10)
ref = {}
for f in FRAMES:
    scene.frame_set(f)
    ref[f] = dict(box=box.matrix_world.copy(), cam=cam.matrix_world.copy(),
                  lens=cam.data.lens, arm=world_verts(skinned),
                  box_verts=basis_world(box))
scene.frame_set(EXPORT_FRAME)
print("exporting with the playhead at frame %d" % EXPORT_FRAME)

maya_scene_importer.register()
out = os.path.join(folder, "roundtrip.ma")
res = bpy.ops.export_scene.maya_ascii(filepath=out)
check("export finished", res == {'FINISHED'})
text = open(out).read()


# -- the file itself -----------------------------------------------------------
parsed = ma_parser.parse_file(out)
model = ma_model.extract(parsed)
check("time unit is pal for 25 fps", parsed.time_unit == 'pal')
check("no lambert left", 'createNode lambert' not in text)

mats = model.materials()
md = mats.get("CrateSG")
check("material exported as standardSurface",
      md is not None and md.node_type == 'standardSurface')
if md:
    check("metalness 0.7", abs(md.metalness - 0.7) < 1e-6)
    check("opacity 0.5", close(md.transparency, (0.5, 0.5, 0.5)))
    check("emission colour and strength",
          close(md.incandescence, (1, 0.5, 0)) and
          abs(md.emission_strength - 2.0) < 1e-6)
    check("base colour texture", md.maya_textures.get('baseColor', '')
          .endswith("crate_albedo.png"))
    check("roughness texture through Separate Color",
          md.maya_textures.get('roughness', '').endswith("crate_packed.png")
          and '_packed.ocg" "Crate.specularRoughness"' in text)
    check("normal map through bump2d (tangent)",
          md.maya_textures.get('normal', '').endswith("crate_normal.png")
          and md.normal_mode == 1)
check("non-colour maps marked Raw", text.count('".cs" -type "string" "Raw"') == 2)

cams = {c['name']: c for c in model.cameras()}
c = cams.get("Cam")
check("camera exported", c is not None)
if c:
    check("camera focal is the export frame's",
          abs(c['focal'] - ref[EXPORT_FRAME]['lens']) < 1e-4)
    check("camera sensor 36 mm", abs(c['sensor_width'] - 36) < 1e-3)

lts = {l['name']: l for l in model.lights()}
check("spot light: intensity, cone, colour",
      'Spot' in lts and lts['Spot']['btype'] == 'SPOT'
      and abs(lts['Spot']['energy'] - 0.5) < 1e-6
      and abs(lts['Spot']['spot_angle'] - 45) < 1e-4
      and close(lts['Spot']['color'], (1, 0.9, 0.8)))
check("sun light", 'Sun' in lts and lts['Sun']['btype'] == 'SUN'
      and abs(lts['Sun']['energy'] - 3) < 1e-6)

anim = model.animations()
check("box transform animated", set(anim.get("Box", {})) >= {'tx', 'rz'})
check("bone b1 rotation animated", 'rx' in anim.get("b1", {}))
check("root joint carries the armature motion", 'tx' in anim.get("b0", {}))
check("skinned mesh transform left alone", "Arm" not in anim)
check("camera animated", 'ty' in anim.get("Cam", {}))
check("zero-scale object skipped, the rest still exported",
      "Pop" not in anim or EXPORT_FRAME != 1)
check("focal length animated", '"CamShape.fl"' in text)
check("shape key weight animated", '"Box_blendShape.w[0]"' in text)
check("play range stored", "playbackOptions -min 1 -max 10" in text)

# joints: the t/jo chain must agree with the stored bind matrices, in cm
worlds = scene_builder._world_matrices(parsed)
ok = True
for j in ("b0", "b1", "b2"):
    bps = scene_builder._maya_matrix(
        ma_parser.as_floats(parsed.nodes[j].attr_tokens('bps')))
    ok = ok and close([x for r in worlds[j] for x in r],
                      [x for r in bps for x in r], 1e-3)
check("joint t/jo chain matches bind matrices", ok)
check("b2 sits 200 cm above b0 in Maya",
      abs((worlds["b2"].translation - worlds["b0"].translation).length - 200)
      < 1e-3)
check("skinCluster has bind pre-matrices", '.pm[2]" -type "matrix"' in text)
check("deformer chain from Orig shape",
      'connectAttr "ArmShapeOrig.w" "Arm_skinCluster.ip[0].ig"' in text and
      'connectAttr "BoxShapeOrig.w" "Box_blendShape.ip[0].ig"' in text)


# -- import it back -----------------------------------------------------------
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.import_scene.maya_ascii(filepath=out)
scene = bpy.context.scene
objs = {o.name: o for o in scene.objects}
check("re-import has Box, Arm, Cam, Spot, Sun",
      all(n in objs for n in ("Box", "Arm", "Cam", "Spot", "Sun")))
check("scene fps 25 again", scene.render.fps == 25)

for f in FRAMES:
    scene.frame_set(f)
    if "Box" in objs:
        got = world_verts(objs["Box"])
        # shape key weights are not imported as animation, so compare the
        # basis cube placement through the object matrix instead
        check("frame %d: box placement" % f,
              close([c for v in basis_world(objs["Box"]) for c in v],
                    [c for v in ref[f]['box_verts'] for c in v], 3e-3))
    if "Arm" in objs:
        check("frame %d: skinned arm vertices" % f,
              close([c for v in world_verts(objs["Arm"]) for c in v],
                    [c for v in ref[f]['arm'] for c in v], 3e-3))
    if "Cam" in objs:
        check("frame %d: camera placement" % f,
              close([x for r in objs["Cam"].matrix_world for x in r],
                    [x for r in ref[f]['cam'] for x in r], 2e-3))

if "Box" in objs:
    check("box keeps its material", objs["Box"].active_material is not None
          and objs["Box"].active_material.name == "Crate")
if "Arm" in objs:
    arm2 = objs["Arm"]
    names = [m.name if m else None for m in arm2.data.materials]
    split = [names[p.material_index] for p in arm2.data.polygons]
    check("skinned arm keeps its per-face materials",
          sorted(set(names)) == ["Crate", "Visor"] and
          split == ["Crate"] * 4 + ["Visor"] * 2)
if "Spot" in objs:
    check("spot energy and size back",
          abs(objs["Spot"].data.energy - 500) < 1e-3 and
          abs(objs["Spot"].data.spot_size - math.radians(45)) < 1e-4)
m2 = bpy.data.materials.get("Crate")
if m2:
    b2 = next(n for n in m2.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    check("re-imported metallic 0.7",
          abs(b2.inputs['Metallic'].default_value - 0.7) < 1e-4)
    check("re-imported alpha 0.5",
          abs(b2.inputs['Alpha'].default_value - 0.5) < 1e-4)
    check("re-imported normal map", b2.inputs['Normal'].is_linked)
    check("re-imported base colour texture", b2.inputs['Base Color'].is_linked)

print("\n%d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
