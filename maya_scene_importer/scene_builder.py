# SPDX-License-Identifier: GPL-3.0-or-later
"""
Builds a Blender scene from the data produced by :mod:`ma_model`.

This is the only module that imports ``bpy`` / ``mathutils``.  Everything is
baked into a single converted world space (Maya Y-up -> Blender Z-up, optional
unit scale) so that meshes, the armature and the skin weights all line up and
deformation is correct with every object left at the identity transform.
"""

import math
import os
import re

import bpy
from mathutils import Matrix, Vector, Euler

from .ma_model import extract
from . import ma_parser


# ---------------------------------------------------------------------------
# coordinate conversion
# ---------------------------------------------------------------------------
def _global_matrix(scale, apply_axis):
    if apply_axis:
        # Maya Y-up -> Blender Z-up  (+90 deg about X):  (x, y, z) -> (x, -z, y)
        axis = Matrix(((1, 0, 0, 0),
                       (0, 0, -1, 0),
                       (0, 1, 0, 0),
                       (0, 0, 0, 1)))
    else:
        axis = Matrix.Identity(4)
    return Matrix.Scale(scale, 4) @ axis


def _maya_matrix(flat16):
    """Maya stores a row-vector matrix (basis vectors as rows, translation in
    the last row).  mathutils uses column-vector maths, so transpose."""
    f = flat16
    return Matrix(((f[0], f[4], f[8], f[12]),
                   (f[1], f[5], f[9], f[13]),
                   (f[2], f[6], f[10], f[14]),
                   (f[3], f[7], f[11], f[15])))


_RO_TABLE = ['XYZ', 'YZX', 'ZXY', 'XZY', 'YXZ', 'ZYX']


def _node_local(node):
    """Local matrix of any DAG transform / joint node from its raw attrs."""
    t = ma_parser.as_floats(node.attr_tokens('t'))[:3] or [0, 0, 0]
    r = ma_parser.as_floats(node.attr_tokens('r'))[:3] or [0, 0, 0]
    s = ma_parser.as_floats(node.attr_tokens('s'))[:3]
    s = s if len(s) == 3 else [1, 1, 1]
    ro_t = node.attr_tokens('ro')
    ro = int(float(ro_t[0])) if ro_t else 0
    order = _RO_TABLE[ro] if 0 <= ro < 6 else 'XYZ'

    T = Matrix.Translation(Vector((t[0], t[1], t[2])))
    R = Euler([math.radians(a) for a in r], order).to_matrix().to_4x4()
    S = Matrix.Diagonal(Vector((s[0], s[1], s[2])).to_4d())
    if node.ntype == 'joint':
        jo = ma_parser.as_floats(node.attr_tokens('jo'))[:3] or [0, 0, 0]
        JO = Euler([math.radians(a) for a in jo], 'XYZ').to_matrix().to_4x4()
        return T @ JO @ R @ S
    return T @ R @ S


def _world_matrices(scene):
    """World matrix of every DAG transform / joint, composed in creation order
    (Maya writes parents before children)."""
    world = {}
    for name in scene.order:
        node = scene.nodes[name]
        if node.ntype not in ('joint', 'transform'):
            continue
        local = _node_local(node)
        pw = world.get(node.parent)
        world[name] = (pw @ local) if pw is not None else local
    return world


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def build(filepath, opts, report=None):
    """Import a Maya ASCII scene.  ``opts`` is any object exposing the option
    attributes (the operator itself, or a plain settings object)."""
    def log(level, msg):
        if report:
            report({level}, msg)
        print("[Maya .ma] %s" % msg)

    scene = ma_parser.parse_file(filepath)
    data = extract(scene)
    gm = _global_matrix(getattr(opts, 'global_scale', 0.01),
                        getattr(opts, 'apply_axis', True))
    gm3 = gm.to_3x3()
    basedir = os.path.dirname(filepath)
    base = _sanitize(os.path.splitext(os.path.basename(filepath))[0]) or "MayaScene"

    # one root collection, with tidy sub-collections created on demand
    root = bpy.data.collections.new(base)
    bpy.context.scene.collection.children.link(root)
    _cols = {}

    def col(name):
        if name not in _cols:
            c = bpy.data.collections.new("%s :: %s" % (base, name))
            root.children.link(c)
            _cols[name] = c
        return _cols[name]

    stats = dict(meshes=0, hidden=0, faces=0, bones=0, skins=0, shapes=0,
                 materials=0, controls=0, constraints=0, ik=0)
    node_world = _world_matrices(scene)

    # -- materials --------------------------------------------------------
    mat_lookup = {}
    materials = data.materials() if opts.import_materials else {}
    for sg, md in materials.items():
        try:
            mat_lookup[sg] = _build_material(md, basedir)
            stats['materials'] += 1
        except Exception as exc:                       # never abort the import
            log('WARNING', "material '%s' failed: %s" % (sg, exc))
    mesh_sg = data.mesh_material_map() if opts.import_materials else {}
    helper_sgs = {sg for sg, md in materials.items()
                  if md.node_type == 'lambert' and not md.maya_textures}

    # -- armature ---------------------------------------------------------
    arm_obj = None
    if opts.import_skeleton:
        try:
            arm_obj = _build_armature(data, gm, node_world, col('Skeleton'), base)
            stats['bones'] = len(arm_obj.data.bones)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            log('ERROR', "armature failed: %s" % exc)

    # -- meshes -----------------------------------------------------------
    mesh_objs = {}
    mode = getattr(opts, 'hidden_handling', 'HIDE')
    if opts.import_meshes:
        for md in data.meshes():
            if md.hidden and mode == 'SKIP_HELPERS' and \
                    _is_helper(md, mesh_sg, helper_sgs):
                continue
            hide = md.hidden and mode != 'SHOW'
            target = col('Hidden') if hide else col('Geometry')
            try:
                obj = _build_mesh(md, gm, mat_lookup, mesh_sg, target,
                                  opts.import_normals, hide)
            except Exception as exc:
                log('WARNING', "mesh '%s' failed: %s" % (md.name, exc))
                continue
            mesh_objs[md.name] = obj
            stats['meshes'] += 1
            stats['hidden'] += int(hide)
            stats['faces'] += len(md.faces)

    # -- skin weights -----------------------------------------------------
    influence_joints = set()
    if opts.import_skeleton and opts.import_skin and arm_obj:
        for sk in data.skins():
            obj = mesh_objs.get(sk.mesh)
            if obj is None:
                continue
            try:
                _bind_skin(obj, sk, arm_obj)
                influence_joints.update(sk.influences.values())
                stats['skins'] += 1
            except Exception as exc:
                log('WARNING', "skin '%s' failed: %s" % (sk.name, exc))

    # tidy the armature: split deform vs helper bones into bone collections
    if arm_obj:
        _organize_bones(arm_obj, influence_joints)

    # -- blend shapes -----------------------------------------------------
    if opts.import_meshes and opts.import_blendshapes:
        for mesh_name, targets in data.blendshapes().items():
            obj = mesh_objs.get(mesh_name)
            if obj is None:
                continue
            try:
                stats['shapes'] += _build_shapekeys(obj, targets, gm3)
            except Exception as exc:
                log('WARNING', "shape keys on '%s' failed: %s" % (mesh_name, exc))

    # -- cameras / lights -------------------------------------------------
    if getattr(opts, 'import_cameras', True):
        for cam in data.cameras():
            try:
                _build_camera(cam, gm, node_world, col('Cameras'))
            except Exception as exc:
                log('WARNING', "camera '%s' failed: %s" % (cam.get('name'), exc))
    if getattr(opts, 'import_lights', True):
        for lt in data.lights():
            try:
                _build_light(lt, gm, node_world, col('Lights'))
            except Exception as exc:
                log('WARNING', "light '%s' failed: %s" % (lt.get('name'), exc))

    # -- time-based animation --------------------------------------------
    if getattr(opts, 'import_animation', True):
        try:
            _apply_animation(data.animations(), mesh_objs, arm_obj)
        except Exception as exc:
            log('WARNING', "animation import failed: %s" % exc)

    # -- experimental rig (control curves + constraints + IK) -------------
    if getattr(opts, 'experimental_rig', False) and arm_obj:
        from . import rig_builder
        try:
            rstats = rig_builder.build_rig(data, gm, node_world, arm_obj,
                                           col('Controls'), log)
            stats.update(rstats)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            log('WARNING', "experimental rig failed (skeleton is unaffected): %s"
                % exc)

    log('INFO', "imported %d meshes (%d hidden), %d bones, %d skinned, %d shape "
        "keys, %d materials, %d controls, %d constraints, %d IK"
        % (stats['meshes'], stats['hidden'], stats['bones'], stats['skins'],
           stats['shapes'], stats['materials'], stats['controls'],
           stats['constraints'], stats['ik']))
    return stats


# ---------------------------------------------------------------------------
# mesh
# ---------------------------------------------------------------------------
def _build_mesh(md, gm, mat_lookup, mesh_sg, collection, want_normals, hide):
    mesh = bpy.data.meshes.new(md.name)
    verts = [gm @ Vector(v) for v in md.verts]
    # guard against degenerate / out-of-range face indices
    nv = len(verts)
    faces = [tuple(i for i in f if 0 <= i < nv) for f in md.faces]
    faces = [f for f in faces if len(f) >= 3]
    mesh.from_pydata([v[:] for v in verts], [], faces)
    mesh.update()

    # uv sets
    for set_idx, (set_name, coords) in enumerate(md.uv_sets):
        uv_layer = mesh.uv_layers.new(name=set_name)
        per_face = md.face_uv_sets[set_idx]
        li = 0
        for poly, src_face, uvidx in zip(mesh.polygons, md.faces, per_face):
            n = len(src_face)
            for k in range(poly.loop_total):
                loop_i = poly.loop_start + k
                if uvidx and k < len(uvidx) and uvidx[k] < len(coords):
                    uv_layer.data[loop_i].uv = coords[uvidx[k]]
            li += n

    # smooth shading + sharp edges from Maya hardness
    for poly in mesh.polygons:
        poly.use_smooth = True
    edge_lookup = {}
    for e in mesh.edges:
        edge_lookup[frozenset(e.vertices)] = e
    for a, b, sharp in md.edges:
        if not sharp:
            continue
        e = edge_lookup.get(frozenset((a, b)))
        if e:
            e.use_edge_sharp = True

    # optional explicit custom normals (best effort, per face corner)
    if want_normals and md.normals and len(md.normals) >= len(mesh.loops):
        try:
            gm3 = gm.to_3x3()
            loop_normals = [(gm3 @ Vector(md.normals[i])).normalized()[:]
                            for i in range(len(mesh.loops))]
            mesh.normals_split_custom_set(loop_normals)
        except Exception:
            pass

    mesh.update()

    obj = bpy.data.objects.new(md.name, mesh)
    collection.objects.link(obj)
    if hide:                              # mirror Maya's hidden state
        obj.hide_render = True
        obj.hide_viewport = True

    # material assignment — one slot per Maya object-group, with per-face
    # indices applied for multi-material meshes (e.g. the helmet glass visor)
    slots = mesh_sg.get(md.render_shape) or mesh_sg.get(md.shape) or []
    slot_of_sg = {}
    for sg, faces in slots:
        mat = mat_lookup.get(sg)
        if mat is None:
            continue
        if sg not in slot_of_sg:
            slot_of_sg[sg] = len(mesh.materials)
            mesh.materials.append(mat)
    if len(slot_of_sg) > 1:
        npoly = len(mesh.polygons)
        for sg, faces in slots:
            if not faces or sg not in slot_of_sg:
                continue
            si = slot_of_sg[sg]
            for fidx in faces:
                if 0 <= fidx < npoly:
                    mesh.polygons[fidx].material_index = si
    return obj


# ---------------------------------------------------------------------------
# naming / organisation helpers
# ---------------------------------------------------------------------------
def _sanitize(name):
    """Make a filename safe and friendly for a Blender datablock name."""
    name = re.sub(r'[^\w.\- ]+', '_', name).strip(' _')
    return name[:58]


def _is_helper(md, mesh_sg, helper_sgs):
    """A hidden mesh is a throwaway rig-construction helper when every shading
    engine assigned to it is a flat, untextured control-colour material."""
    sgs = [sg for sg, _ in (mesh_sg.get(md.render_shape)
                            or mesh_sg.get(md.shape) or [])]
    if not sgs:
        return not md.is_skinned        # unassigned + unskinned hidden = helper
    return all(sg in helper_sgs for sg in sgs)


def _organize_bones(arm_obj, influence_joints):
    """Split bones into 'Deform' (skin influences) and 'Rig / Helpers' bone
    collections so the viewport is not buried under control/IK chains."""
    arm = arm_obj.data
    arm_obj.show_in_front = True
    try:
        deform = arm.collections.new("Deform")
        other = arm.collections.new("Rig / Helpers")
    except Exception:
        return
    for b in arm.bones:
        is_deform = b.name in influence_joints
        b.use_deform = is_deform
        (deform if is_deform else other).assign(b)
    # show the deform skeleton by default; tuck the control/IK chains away
    if influence_joints:
        try:
            other.is_visible = False
        except Exception:
            pass


# ---------------------------------------------------------------------------
# armature
# ---------------------------------------------------------------------------
def _build_armature(data, gm, node_world, collection, name):
    joints = data.joints_in_order()
    arm = bpy.data.armatures.new(name)
    arm_obj = bpy.data.objects.new(name, arm)
    collection.objects.link(arm_obj)

    # compute every joint's converted world rest matrix.  The cached bind pose
    # (.bps) is authoritative where present; otherwise fall back to the world
    # matrix composed from the full DAG (so joints parented under plain group
    # transforms still land in the right place).
    world = {}
    for jd in joints:
        if jd.bps:
            world[jd.name] = _maya_matrix(jd.bps)
        else:
            world[jd.name] = node_world.get(jd.name, Matrix.Identity(4))

    view_layer = bpy.context.view_layer
    view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')
    ebs = arm.edit_bones

    created = {}
    for jd in joints:
        eb = ebs.new(jd.name)
        m = gm @ world[jd.name]
        # orthonormalise so the bone rest is a clean rotation+translation
        loc, rot, _ = m.decompose()
        rest = rot.to_matrix().to_4x4()
        rest.translation = loc
        eb.head = (0, 0, 0)
        eb.tail = (0, 1, 0)
        eb.matrix = rest
        created[jd.name] = eb

    # lengths (head -> mean child, else a small default) and parenting, using
    # the nearest joint ancestor so bones reconnect across Maya group nodes
    head_of = {n: created[n].head.copy() for n in created}
    children = {}
    for jd in joints:
        if jd.bone_parent in created:
            children.setdefault(jd.bone_parent, []).append(jd.name)
    for jd in joints:
        eb = created[jd.name]
        kids = children.get(jd.name, [])
        if kids:
            cpos = sum((head_of[k] for k in kids), Vector()) / len(kids)
            length = (cpos - eb.head).length
        else:
            length = 0.0
        if length < 1e-4:
            length = max(0.01, jd.radius * abs(gm.to_scale().x))
        eb.length = length
        if jd.bone_parent in created:
            eb.parent = created[jd.bone_parent]

    bpy.ops.object.mode_set(mode='OBJECT')

    # custom properties (preserve rig metadata on the pose bones)
    for jd in joints:
        if not jd.custom:
            continue
        pb = arm_obj.pose.bones.get(jd.name)
        if pb:
            for k, v in jd.custom.items():
                try:
                    pb[k] = v
                except Exception:
                    pass
    return arm_obj


# ---------------------------------------------------------------------------
# skin
# ---------------------------------------------------------------------------
def _bind_skin(obj, skin, arm_obj):
    # group weights by joint for fewer API calls
    by_joint = {}
    for vtx, wmap in skin.weights.items():
        if vtx >= len(obj.data.vertices):
            continue
        for joint, w in wmap.items():
            by_joint.setdefault(joint, []).append((vtx, w))

    for joint, pairs in by_joint.items():
        vg = obj.vertex_groups.get(joint) or obj.vertex_groups.new(name=joint)
        # add in small batches grouped by identical weight is overkill; add per vert
        for vtx, w in pairs:
            vg.add([vtx], w, 'REPLACE')

    mod = obj.modifiers.new("Armature", 'ARMATURE')
    mod.object = arm_obj
    mod.use_deform_preserve_volume = False
    if obj.parent is None:
        obj.parent = arm_obj


# ---------------------------------------------------------------------------
# blend shapes
# ---------------------------------------------------------------------------
def _build_shapekeys(obj, targets, gm3):
    mesh = obj.data
    if not obj.data.shape_keys:
        obj.shape_key_add(name="Basis", from_mix=False)
    n = 0
    for tgt in targets:
        sk = obj.shape_key_add(name=tgt.name, from_mix=False)
        base = sk.data
        for vi, delta in tgt.deltas.items():
            if vi < len(base):
                d = gm3 @ Vector(delta)
                base[vi].co = base[vi].co + d
        n += 1
    return n


# ---------------------------------------------------------------------------
# materials
# ---------------------------------------------------------------------------
_SLOTS = {
    'baseColor': ('BaseColor', 'Albedo', 'Diffuse', 'Color', 'BaseColour'),
    'metallic': ('Metallic', 'Metalness', 'Metal'),
    'roughness': ('Roughness', 'Rough'),
    'normal': ('Normal', 'NormalMap', 'Nrm'),
    'emissive': ('Emissive', 'Emission', 'Glow'),
}


def _find_texture(basedir, prefix, names):
    if not prefix:
        return None
    try:
        listing = os.listdir(basedir)
    except OSError:
        return None
    lowered = {f.lower(): f for f in listing}
    for slot_name in names:
        for ext in ('.png', '.jpg', '.jpeg', '.tga', '.tif', '.tiff', '.exr',
                    '.bmp', '.psd'):
            cand = (prefix + '_' + slot_name + ext).lower()
            if cand in lowered:
                return os.path.join(basedir, lowered[cand])
    return None


def _load_image(path, non_color):
    if not path or not os.path.exists(path):
        return None
    img = bpy.data.images.load(path, check_existing=True)
    if non_color:
        try:
            img.colorspace_settings.name = 'Non-Color'
        except Exception:
            pass
    return img


def _build_material(md, basedir):
    mat = bpy.data.materials.new(md.name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (600, 0)
    bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (250, 0)
    nt.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

    # base colour
    base = bsdf.inputs.get('Base Color')
    if base:
        base.default_value = (md.color[0], md.color[1], md.color[2], 1.0)

    x = -650
    prefix = md.tex_prefix

    def add_tex(slot, non_color):
        nonlocal x
        path = _find_texture(basedir, prefix, _SLOTS[slot])
        if not path and slot == 'baseColor' and 'baseColor' in md.maya_textures:
            # fall back to the exact map referenced by Maya (remapped to basedir)
            fn = re.split(r'[\\/]', md.maya_textures['baseColor'])[-1]
            try:
                names = {f.lower(): f for f in os.listdir(basedir)}
            except OSError:
                names = {}
            if fn.lower() in names:       # case-insensitive, as on Windows
                path = os.path.join(basedir, names[fn.lower()])
        img = _load_image(path, non_color)
        if not img:
            return None
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image = img
        tex.location = (x, 0)
        x_local = x
        return tex

    # Base Color
    tex = add_tex('baseColor', non_color=False)
    if tex:
        tex.location = (-650, 350)
        nt.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])

    # Metallic
    tex = add_tex('metallic', non_color=True)
    if tex:
        tex.location = (-650, 100)
        nt.links.new(tex.outputs['Color'], bsdf.inputs['Metallic'])

    # Roughness
    tex = add_tex('roughness', non_color=True)
    if tex:
        tex.location = (-650, -150)
        nt.links.new(tex.outputs['Color'], bsdf.inputs['Roughness'])

    # Emission
    tex = add_tex('emissive', non_color=False)
    if tex:
        tex.location = (-650, -400)
        em = bsdf.inputs.get('Emission Color')
        if em:
            nt.links.new(tex.outputs['Color'], em)
            if 'Emission Strength' in bsdf.inputs:
                bsdf.inputs['Emission Strength'].default_value = 1.0
    elif any(md.incandescence):
        em = bsdf.inputs.get('Emission Color')
        if em:
            em.default_value = (md.incandescence[0], md.incandescence[1],
                                md.incandescence[2], 1.0)
            if 'Emission Strength' in bsdf.inputs:
                bsdf.inputs['Emission Strength'].default_value = 1.0

    # Normal
    npath = _find_texture(basedir, prefix, _SLOTS['normal'])
    nimg = _load_image(npath, non_color=True)
    if nimg:
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image = nimg
        tex.location = (-650, -650)
        nmap = nt.nodes.new('ShaderNodeNormalMap')
        nmap.location = (-300, -650)
        nt.links.new(tex.outputs['Color'], nmap.inputs['Color'])
        nt.links.new(nmap.outputs['Normal'], bsdf.inputs['Normal'])

    # glass-ish material
    if md.node_type == 'blinn' and 'glass' in md.name.lower():
        if 'Transmission Weight' in bsdf.inputs:
            bsdf.inputs['Transmission Weight'].default_value = 1.0
        if 'Roughness' in bsdf.inputs:
            bsdf.inputs['Roughness'].default_value = 0.0

    return mat


# ---------------------------------------------------------------------------
# cameras / lights / animation
# ---------------------------------------------------------------------------
def _placed(obj, name, gm, node_world, collection):
    wm = node_world.get(name)
    obj.matrix_world = (gm @ wm) if wm is not None else gm
    collection.objects.link(obj)


def _build_camera(cam, gm, node_world, collection):
    data = bpy.data.cameras.new(cam['name'])
    data.type = 'ORTHO' if cam.get('ortho') else 'PERSP'
    data.lens = cam.get('focal', 35.0)
    obj = bpy.data.objects.new(cam['name'], data)
    # Maya camera looks down -Z like Blender, but its transform already encodes
    # this; place it at the converted world matrix
    _placed(obj, cam['name'], gm, node_world, collection)


def _build_light(lt, gm, node_world, collection):
    data = bpy.data.lights.new(lt['name'], lt['btype'])
    try:
        data.color = lt['color']
    except Exception:
        pass
    if lt['btype'] == 'SUN':
        data.energy = lt['energy']
    else:
        data.energy = lt['energy'] * 1000.0      # Maya intensity -> watts-ish
    if lt['btype'] == 'SPOT':
        data.spot_size = math.radians(lt.get('spot_angle', 40.0))
    obj = bpy.data.objects.new(lt['name'], data)
    _placed(obj, lt['name'], gm, node_world, collection)


def _apply_animation(anim, mesh_objs, arm_obj):
    """Apply time-based animCurves to imported objects / pose bones.

    Uses ``keyframe_insert`` (rather than the Action F-curve API) so it works
    across Blender's classic and current 'slotted' animation systems."""
    if not anim:
        return
    bone_lookup = {b.name: b for b in arm_obj.pose.bones} if arm_obj else {}

    for node_name, channels in anim.items():
        pbone = bone_lookup.get(node_name)
        if pbone is not None:
            owner, anim_obj = pbone, arm_obj
            prefix = 'pose.bones["%s"].' % node_name
        else:
            owner = mesh_objs.get(node_name) or bpy.data.objects.get(node_name)
            if owner is None:
                continue
            anim_obj, prefix = owner, ''
        for path, idx, pts in channels:
            for frame, value in pts:
                try:
                    getattr(owner, path)[idx] = value
                    anim_obj.keyframe_insert(data_path=prefix + path,
                                             index=idx, frame=frame)
                except Exception:
                    pass
