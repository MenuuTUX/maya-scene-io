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

import functools
import zlib

import bpy
import numpy as np
from mathutils import Matrix, Vector, Euler

from .ma_model import extract
from . import ma_parser


def bl_name(name):
    """A Maya node name as a Blender bone / vertex group name.  Blender cuts
    names at 63 bytes, which would unhook a long namespaced joint from its skin
    weights and keys, so longer names are cut here and given a hash instead;
    the hash keeps two long names with the same start apart."""
    raw = name.encode('utf-8')
    if len(raw) <= 63:
        return name
    tag = '%08x' % (zlib.crc32(raw) & 0xffffffff)
    return raw[:54].decode('utf-8', 'ignore') + '_' + tag


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


def _triple(values, default):
    """Three finite numbers from a value list that may be short or damaged."""
    out = [v if math.isfinite(v) else d for v, d in zip(values, default)]
    return out + list(default[len(out):])


def _node_local(node, scene, ov=None):
    """Local matrix of any DAG transform / joint node from its raw attrs.
    ``ov`` overrides single channels ({'tx': 3.0, 'rz': 90.0, ...}) in cm and
    degrees, which is how animation is evaluated."""
    lin, ang = scene.cm_per_unit, scene.deg_per_unit
    t = [v * lin for v in
         _triple(ma_parser.as_floats(node.attr_tokens('t')), (0, 0, 0))]
    r = [v * ang for v in
         _triple(ma_parser.as_floats(node.attr_tokens('r')), (0, 0, 0))]
    s = _triple(ma_parser.as_floats(node.attr_tokens('s')), (1, 1, 1))
    if ov:
        for chan, vec in (('t', t), ('r', r), ('s', s)):
            for i, ax in enumerate('xyz'):
                if chan + ax in ov:
                    vec[i] = ov[chan + ax]
    ro_t = node.attr_tokens('ro')
    try:
        ro = int(float(ro_t[0])) if ro_t else 0
    except (ValueError, OverflowError):
        ro = 0
    order = _RO_TABLE[ro] if 0 <= ro < 6 else 'XYZ'

    T = Matrix.Translation(Vector((t[0], t[1], t[2])))
    R = Euler([math.radians(a) for a in r], order).to_matrix().to_4x4()
    S = Matrix.Diagonal(Vector((s[0], s[1], s[2])).to_4d())
    if node.ntype == 'joint':
        jo = _triple(ma_parser.as_floats(node.attr_tokens('jo')), (0, 0, 0))
        JO = Euler([math.radians(a * ang) for a in jo],
                   'XYZ').to_matrix().to_4x4()
        return T @ JO @ R @ S

    # Maya's full transform: rotate and scale about their pivots, plus the
    # pivot translates Maya adds to keep an object in place when its pivot
    # moves, the rotate axis and shear.  In column-vector order:
    #   T . RPT . RP . R . RA . RP^-1 . SPT . SP . SH . S . SP^-1
    def vec(attr):
        v = _triple(ma_parser.as_floats(node.attr_tokens(attr)), (0, 0, 0))
        return Vector((v[0] * lin, v[1] * lin, v[2] * lin))
    rp, sp = vec('rp'), vec('sp')
    ra = _triple(ma_parser.as_floats(node.attr_tokens('ra')), (0, 0, 0))
    RA = Euler([math.radians(a * ang) for a in ra], 'XYZ').to_matrix().to_4x4()
    xy, xz, yz = _triple(ma_parser.as_floats(node.attr_tokens('sh')), (0, 0, 0))
    SH = Matrix(((1, xy, xz, 0), (0, 1, yz, 0), (0, 0, 1, 0), (0, 0, 0, 1)))
    return (T @ Matrix.Translation(vec('rpt')) @ Matrix.Translation(rp)
            @ R @ RA @ Matrix.Translation(-rp)
            @ Matrix.Translation(vec('spt')) @ Matrix.Translation(sp)
            @ SH @ S @ Matrix.Translation(-sp))


def _convert(gm, world):
    """Maya world matrix -> Blender world matrix.  The unit scale applies to
    positions only, so an object keeps the scale it had in Maya."""
    s = gm.to_scale().x
    m = Matrix.Scale(1.0 / s, 4) @ gm @ world
    m.translation = m.translation * s
    return m


def _ortho(m):
    """Drop scale and shear: rotation + translation only."""
    loc, rot, _ = m.decompose()
    out = rot.to_matrix().to_4x4()
    out.translation = loc
    return out


_FPS = {'game': 15, 'film': 24, 'pal': 25, 'ntsc': 30, 'show': 48,
        'palf': 50, 'ntscf': 60}


def _fps(unit):
    if unit in _FPS:
        return float(_FPS[unit])
    m = re.match(r'([\d.]+)fps$', unit or '')
    return float(m.group(1)) if m else None


def _world_matrices(scene):
    """World matrix of every DAG transform / joint, composed in creation order
    (Maya writes parents before children)."""
    world = {}
    for name in scene.order:
        node = scene.nodes[name]
        if node.ntype not in ('joint', 'transform'):
            continue
        local = _node_local(node, scene)
        pw = world.get(node.parent)
        world[name] = (pw @ local) if pw is not None else local
    return world


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def build(filepath, opts, report=None, parse=None):
    """Import a Maya scene.  ``opts`` is any object exposing the option
    attributes (the operator itself, or a plain settings object).  ``parse``
    reads the file into a MayaScene (Maya ASCII when not given).  Shows a
    progress bar while it works."""
    manager = bpy.context.window_manager
    active = bpy.context.view_layer.objects.active
    if active is not None and active.mode != 'OBJECT':
        # the armature step switches modes, which fails from another mode
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except RuntimeError:
            pass
    manager.progress_begin(0, 100)
    try:
        return _build(filepath, opts, report, manager.progress_update, parse)
    finally:
        manager.progress_end()


def _build(filepath, opts, report, progress, parse=None):
    def log(level, msg):
        if report:
            report({level}, msg)
        print("[Maya .ma] %s" % msg)

    try:
        size_mb = os.path.getsize(filepath) / 1e6
    except OSError:
        size_mb = 0
    if size_mb > 500:
        log('WARNING', "%.0f MB scene: parsing keeps roughly 5x the file size in "
            "memory, so close other applications first" % size_mb)

    _listing.cache_clear()           # folders may have changed since last import
    scene = (parse or ma_parser.parse_file)(filepath)
    if not scene.nodes:
        raise ValueError("no Maya nodes found: the file is empty or is not a "
                         "Maya ASCII scene")
    if scene.references:
        log('WARNING', "this scene references %d other file(s) that are not "
            "loaded (%s)" % (len(scene.references),
                             ", ".join(os.path.basename(r)
                                       for r in scene.references[:3])))
    if scene.skipped:
        log('WARNING', "%d damaged statement(s) were skipped; the file may be "
            "truncated or corrupt" % scene.skipped)
    for note in getattr(scene, 'notes', ()):
        log('WARNING', note)
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
                 materials=0, controls=0, ik=0)
    node_world = _world_matrices(scene)

    progress(15)
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

    progress(30)
    # -- armature ---------------------------------------------------------
    arm_obj = None
    if opts.import_skeleton:
        try:
            arm_obj = _build_armature(data, gm, node_world, col('Skeleton'), base,
                                      getattr(opts, 'orient_bones', True))
            stats['bones'] = len(arm_obj.data.bones)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            log('ERROR', "armature failed: %s" % exc)

    progress(40)
    # -- meshes -----------------------------------------------------------
    mesh_objs = {}
    placed = {}              # camera / light transform name -> object
    mesh_world = {}          # transform name -> Maya world matrix baked in
    mode = getattr(opts, 'hidden_handling', 'HIDE')
    if opts.import_meshes:
        for md in data.meshes():
            if md.hidden and mode == 'SKIP_HELPERS' and \
                    _is_helper(md, mesh_sg, helper_sgs):
                continue
            hide = md.hidden and mode != 'SHOW'
            target = col('Hidden') if hide else col('Geometry')
            # A skinned mesh lands where its skin's geometry matrix puts it;
            # any other mesh sits at its transform's world matrix.
            if md.is_skinned:
                wm = _maya_matrix(md.skin_geom) if md.skin_geom \
                    else Matrix.Identity(4)
            else:
                wm = node_world.get(md.name, Matrix.Identity(4))
            try:
                obj = _build_mesh(md, gm, wm, mat_lookup, mesh_sg, target,
                                  hide)
            except Exception as exc:
                log('WARNING', "mesh '%s' failed: %s" % (md.name, exc))
                continue
            mesh_objs[md.name] = obj
            mesh_world[md.name] = wm
            if arm_obj and not md.is_skinned:
                _attach_to_joint(obj, md.name, data, arm_obj)
            stats['meshes'] += 1
            stats['hidden'] += int(hide)
            stats['faces'] += len(md.faces)

    progress(75)
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

    progress(85)
    # -- blend shapes -----------------------------------------------------
    if opts.import_meshes and opts.import_blendshapes:
        for mesh_name, targets in data.blendshapes().items():
            obj = mesh_objs.get(mesh_name)
            if obj is None:
                continue
            try:
                stats['shapes'] += _build_shapekeys(
                    obj, targets,
                    gm3 @ mesh_world.get(mesh_name, Matrix.Identity(4)).to_3x3())
            except Exception as exc:
                log('WARNING', "shape keys on '%s' failed: %s" % (mesh_name, exc))

    progress(92)
    # -- cameras / lights -------------------------------------------------
    if getattr(opts, 'import_cameras', True):
        for cam in data.cameras():
            try:
                placed[cam['name']] = _build_camera(
                    cam, gm, node_world, col('Cameras'))
            except Exception as exc:
                log('WARNING', "camera '%s' failed: %s" % (cam.get('name'), exc))
    if getattr(opts, 'import_lights', True):
        for lt in data.lights():
            try:
                placed[lt['name']] = _build_light(
                    lt, gm, node_world, col('Lights'))
            except Exception as exc:
                log('WARNING', "light '%s' failed: %s" % (lt.get('name'), exc))

    progress(95)
    # -- time-based animation --------------------------------------------
    if getattr(opts, 'import_animation', True):
        try:
            span = _apply_animation(data, node_world, gm, arm_obj,
                                    {**mesh_objs, **placed}, mesh_world, log)
            fps = _fps(scene.time_unit)
            if fps:
                bpy.context.scene.render.fps = int(round(fps))
            if span:
                lo, hi = scene.play_range or span
                bpy.context.scene.frame_start = int(lo)
                bpy.context.scene.frame_end = int(max(hi, lo))
                # open on the frame the file was saved on, as Maya does
                if scene.current_frame is not None:
                    bpy.context.scene.frame_set(int(round(scene.current_frame)))
        except Exception as exc:
            import traceback
            traceback.print_exc()
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

    for warning in data.warnings:
        log('WARNING', warning)
    log('INFO', "imported %d meshes (%d hidden), %d bones, %d skinned, %d shape "
        "keys, %d materials, %d controls, %d IK"
        % (stats['meshes'], stats['hidden'], stats['bones'], stats['skins'],
           stats['shapes'], stats['materials'], stats['controls'],
           stats['ik']))
    return stats


# ---------------------------------------------------------------------------
# mesh
# ---------------------------------------------------------------------------
def _build_mesh(md, gm, wm, mat_lookup, mesh_sg, collection, hide):
    mesh = bpy.data.meshes.new(md.name)
    full = gm @ wm
    m = np.array(full)
    nv = len(md.verts)
    co = np.array(md.verts, dtype=np.float64).reshape(-1, 3) @ m[:3, :3].T \
        + m[:3, 3]
    # a mirroring transform (negative scale) turns every face inside out
    flip = wm.to_3x3().determinant() < 0
    # guard against degenerate / out-of-range face indices; `kept` remembers
    # which source faces survived so UVs stay lined up with the polygons
    kept, loops = [], []
    for fi, f in enumerate(md.faces):
        f = [i for i in (f[::-1] if flip else f) if 0 <= i < nv]
        if len(f) >= 3:
            kept.append(fi)
            loops.append(f)
    totals = np.array([len(f) for f in loops], dtype=np.int32)
    starts = np.cumsum(totals, dtype=np.int32) - totals
    nloops = int(totals.sum())

    mesh.vertices.add(nv)
    mesh.vertices.foreach_set('co', co.ravel())
    mesh.loops.add(nloops)
    mesh.loops.foreach_set('vertex_index',
                           np.fromiter((i for f in loops for i in f),
                                       dtype=np.int32, count=nloops))
    mesh.polygons.add(len(loops))
    mesh.polygons.foreach_set('loop_start', starts)
    mesh.polygons.foreach_set('loop_total', totals)
    mesh.update(calc_edges=True)
    face_at = None if len(kept) == len(md.faces) \
        else {src: i for i, src in enumerate(kept)}

    def corner_values(per_face, values, width, fill):
        """One row of ``width`` numbers per face corner, taken from ``values``
        through each face's index list (Maya numbers UVs and colours per
        corner); corners without an index get ``fill``."""
        index = np.full(nloops, -1, dtype=np.int64)
        for start, total, fi in zip(starts, totals, kept):
            ids = per_face[fi]
            if ids:
                if flip:
                    ids = ids[::-1]
                n = min(total, len(ids))
                index[start:start + n] = ids[:n]
        valid = (index >= 0) & (index < len(values))
        out = np.tile(np.array(fill, dtype=np.float64), (nloops, 1))
        out[valid] = np.array(values, dtype=np.float64)[index[valid]]
        return out

    # uv sets
    for set_idx, (set_name, coords) in enumerate(md.uv_sets):
        uv_layer = mesh.uv_layers.new(name=set_name)
        uvs = corner_values(md.face_uv_sets[set_idx], coords, 2, (0.0, 0.0))
        uv_layer.data.foreach_set('uv', uvs.ravel())

    # colour sets (vertex colours), as per-corner attributes
    for set_name, colors, per_face in md.color_sets:
        try:
            attr = mesh.color_attributes.new(name=set_name, type='FLOAT_COLOR',
                                             domain='CORNER')
            attr.data.foreach_set(
                'color', corner_values(per_face, colors, 4,
                                       (1.0, 1.0, 1.0, 1.0)).ravel())
        except (RuntimeError, TypeError) as exc:
            print("[Maya .ma] colour set '%s' on '%s' skipped: %s"
                  % (set_name, md.name, exc))
    if md.color_sets and mesh.color_attributes:
        try:
            mesh.color_attributes.active_color = mesh.color_attributes[
                min(md.active_color, len(mesh.color_attributes) - 1)]
        except (RuntimeError, TypeError, IndexError):
            pass

    # smooth shading + sharp edges from Maya hardness
    mesh.polygons.foreach_set('use_smooth', np.ones(len(loops), dtype=bool))
    sharp = [(a, b) for a, b, hard in md.edges if hard]
    if sharp and len(mesh.edges):
        ev = np.empty(len(mesh.edges) * 2, dtype=np.int64)
        mesh.edges.foreach_get('vertices', ev)
        ev = ev.reshape(-1, 2)
        pairs = np.array(sharp, dtype=np.int64)

        def key(e):                       # one number per undirected edge
            return e.min(axis=1) * nv + e.max(axis=1)
        mesh.edges.foreach_set('use_edge_sharp', np.isin(key(ev), key(pairs)))
    if mesh.validate():
        print("[Maya .ma] mesh '%s' had invalid geometry that was repaired"
              % md.name)

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
                if face_at is not None:       # some faces were dropped above
                    fidx = face_at.get(fidx, -1)
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
    deform_names = {bl_name(j) for j in influence_joints}
    arm_obj.show_in_front = True
    try:
        deform = arm.collections.new("Deform")
        other = arm.collections.new("Rig / Helpers")
    except Exception:
        return
    for b in arm.bones:
        is_deform = b.name in deform_names
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
# Blender bones point along their local +Y, Maya joints usually along +X (or
# -X on a mirrored side).  Each bone's frame is its Maya joint frame times a
# quarter-turn permutation, chosen per bone so +Y aims at the child.  The tag
# is stored on the bone so the exporter and the animation import can undo it.
def bone_axis_matrix(tag):
    """4x4 rotation taking bone +Y onto the Maya joint axis named by ``tag``."""
    r90 = math.pi / 2
    if tag == '+X':
        return Matrix.Rotation(-r90, 4, 'Z')
    if tag == '-X':
        return Matrix.Rotation(r90, 4, 'Z')
    if tag == '+Z':
        return Matrix.Rotation(r90, 4, 'X')
    if tag == '-Z':
        return Matrix.Rotation(-r90, 4, 'X')
    if tag == '-Y':
        return Matrix.Rotation(math.pi, 4, 'Z')
    return Matrix.Identity(4)


def _aim_axis(local_dir):
    """Signed joint axis ('+X', '-Y', ...) closest to a joint-local direction."""
    comp = max(range(3), key=lambda i: abs(local_dir[i]))
    return ('+' if local_dir[comp] >= 0 else '-') + 'XYZ'[comp]


def _build_armature(data, gm, node_world, collection, name, orient=True):
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

    # orthonormalised joint frames, so a bone rest is a clean rotation+translation
    frames = {}
    for jd in joints:
        loc, rot, _ = (gm @ world[jd.name]).decompose()
        frames[jd.name] = (rot.to_matrix(), loc)
    children = {}
    for jd in joints:
        if jd.bone_parent in frames:
            children.setdefault(jd.bone_parent, []).append(jd.name)

    # per-bone aim axis (joints come parent first, so leaves can inherit)
    axis_of = {}
    for jd in joints:
        tag = '+Y'
        if orient:
            kids = children.get(jd.name, [])
            rot3, loc = frames[jd.name]
            if kids:
                mean = sum((frames[k][1] for k in kids), Vector()) / len(kids)
                direction = mean - loc
            else:
                direction = Vector()
            if direction.length > 1e-6:
                tag = _aim_axis(rot3.transposed() @ direction)
            else:
                tag = axis_of.get(jd.bone_parent, '+Y')
        axis_of[jd.name] = tag

    created = {}
    for jd in joints:
        eb = ebs.new(bl_name(jd.name))
        rot3, loc = frames[jd.name]
        rest = rot3.to_4x4() @ bone_axis_matrix(axis_of[jd.name])
        rest.translation = loc
        eb.head = (0, 0, 0)
        eb.tail = (0, 1, 0)
        eb.matrix = rest
        eb['maya_axis'] = axis_of[jd.name]
        created[jd.name] = eb

    # lengths (head -> mean child, else a small default) and parenting, using
    # the nearest joint ancestor so bones reconnect across Maya group nodes
    head_of = {n: created[n].head.copy() for n in created}
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
        pb = arm_obj.pose.bones.get(bl_name(jd.name))
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
        group = bl_name(joint)
        vg = obj.vertex_groups.get(group) or obj.vertex_groups.new(name=group)
        # add in small batches grouped by identical weight is overkill; add per vert
        for vtx, w in pairs:
            vg.add([vtx], w, 'REPLACE')

    mod = obj.modifiers.new("Armature", 'ARMATURE')
    mod.object = arm_obj
    mod.use_deform_preserve_volume = False
    if obj.parent is None:
        obj.parent = arm_obj


def _attach_to_joint(obj, transform_name, data, arm_obj):
    """A mesh that hangs under a joint in Maya (a prop on a hand, a rigid
    armour plate) follows that bone.  The mesh is already baked at its rest
    position, so the parent inverse cancels the bone's rest matrix."""
    joint = data._nearest_joint_ancestor(transform_name)
    bone = arm_obj.data.bones.get(bl_name(joint)) if joint else None
    if bone is None:
        return
    obj.parent = arm_obj
    obj.parent_type = 'BONE'
    obj.parent_bone = bone.name
    # Blender attaches a bone child at the bone's tail
    tail = Matrix.Translation((0, bone.length, 0))
    obj.matrix_parent_inverse = (arm_obj.matrix_world @ bone.matrix_local
                                 @ tail).inverted()


# ---------------------------------------------------------------------------
# blend shapes
# ---------------------------------------------------------------------------
def _build_shapekeys(obj, targets, gm3):
    if not obj.data.shape_keys:
        obj.shape_key_add(name="Basis", from_mix=False)
    nv = len(obj.data.vertices)
    m = np.array(gm3)
    for tgt in targets:
        sk = obj.shape_key_add(name=tgt.name, from_mix=False)
        sk.value = max(0.0, min(1.0, tgt.weight))
        idx = np.fromiter((v for v in tgt.deltas if v < nv), dtype=np.int64)
        if len(idx):
            co = np.empty(nv * 3, dtype=np.float32)
            sk.data.foreach_get('co', co)
            co = co.reshape(-1, 3).astype(np.float64)
            co[idx] += np.array([tgt.deltas[v] for v in idx]) @ m.T
            sk.data.foreach_set('co', co.ravel())
    return len(targets)


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


_IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.tga', '.tif', '.tiff', '.exr', '.bmp',
               '.psd')
_IMAGE_DIRS = ('', 'sourceimages', 'textures', 'images', 'maps')


@functools.lru_cache(maxsize=32)
def _listing(basedir):
    """{lower-case name: real name} for a folder, case-insensitive as on Windows."""
    try:
        return {f.lower(): f for f in os.listdir(basedir)}
    except OSError:
        return {}


def _resolve_texture(basedir, maya_path):
    """The image a Maya file node names.  Tried in turn: the path as written
    (the same machine), then the file name in the scene's folder, its sibling
    project folders (sourceimages, textures, ...) and the folder tail the path
    gives, all case-insensitively as on Windows."""
    if not maya_path:
        return None
    if maya_path.lower().endswith(_IMAGE_EXTS) and os.path.isfile(maya_path):
        return maya_path
    parts = [p for p in maya_path.replace('\\', '/').split('/')
             if p and not p.endswith(':')]
    if not parts:
        return None
    name = parts[-1].lower()
    for root in dict.fromkeys((basedir, os.path.dirname(basedir))):
        folders = [os.path.join(root, d) if d else root for d in _IMAGE_DIRS]
        folders += [os.path.join(root, *parts[k:-1])
                    for k in range(len(parts) - 1)]
        for folder in folders:
            hit = _listing(folder).get(name)
            if hit:
                return os.path.join(folder, hit)
    return None


def _find_texture(basedir, prefix, names):
    if not prefix:
        return None
    lowered = _listing(basedir)
    for slot_name in names:
        for ext in _IMAGE_EXTS:
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

    prefix = md.tex_prefix

    def add_tex(slot, non_color):
        path = _find_texture(basedir, prefix, _SLOTS[slot])
        if not path and slot in md.maya_textures:
            # no map by the usual name: the one the file node in Maya names
            path = _resolve_texture(basedir, md.maya_textures[slot])
        img = _load_image(path, non_color)
        if not img:
            return None
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image = img
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
    mode = 1                              # a map named *_Normal is a normal map
    if not npath and 'normal' in md.maya_textures:
        npath = _resolve_texture(basedir, md.maya_textures['normal'])
        mode = md.normal_mode             # Maya says: 0 bump, 1 tangent, 2 object
    nimg = _load_image(npath, non_color=True)
    if nimg:
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image = nimg
        tex.location = (-650, -650)
        if mode == 0:
            node = nt.nodes.new('ShaderNodeBump')
            node.inputs['Strength'].default_value = md.bump_depth
            nt.links.new(tex.outputs['Color'], node.inputs['Height'])
        else:
            node = nt.nodes.new('ShaderNodeNormalMap')
            node.space = 'OBJECT' if mode == 2 else 'TANGENT'
            nt.links.new(tex.outputs['Color'], node.inputs['Color'])
        node.location = (-300, -650)
        nt.links.new(node.outputs['Normal'], bsdf.inputs['Normal'])

    # Maya transparency (a colour, 1 = fully see-through) becomes alpha
    opacity = 1.0 - sum(md.transparency) / 3.0
    if opacity < 0.999 and 'Alpha' in bsdf.inputs:
        bsdf.inputs['Alpha'].default_value = max(0.0, opacity)
        try:
            mat.surface_render_method = 'BLENDED'
        except AttributeError:            # Blender before 4.2
            mat.blend_method = 'BLEND'

    return mat


# ---------------------------------------------------------------------------
# cameras / lights / animation
# ---------------------------------------------------------------------------
def _placed(obj, name, gm, node_world, collection):
    wm = node_world.get(name)
    obj.matrix_world = _convert(gm, wm) if wm is not None else gm
    collection.objects.link(obj)


def _build_camera(cam, gm, node_world, collection):
    data = bpy.data.cameras.new(cam['name'])
    data.type = 'ORTHO' if cam.get('ortho') else 'PERSP'
    data.lens = cam.get('focal', 35.0)
    scale = gm.to_scale().x
    if cam.get('sensor_width'):
        data.sensor_width = cam['sensor_width']
    if cam.get('ortho'):
        data.ortho_scale = max(cam.get('ortho_width', 30.0) * scale, 1e-6)
    data.clip_start = max(cam.get('near', 0.1) * scale, 1e-6)
    data.clip_end = max(cam.get('far', 10000.0) * scale, data.clip_start * 1.01)
    obj = bpy.data.objects.new(cam['name'], data)
    # Maya camera looks down -Z like Blender, but its transform already encodes
    # this; place it at the converted world matrix
    _placed(obj, cam['name'], gm, node_world, collection)
    return obj


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
    return obj


def _sample(keys, frame):
    """Linear sample of [(frame, value)] keys, held beyond both ends."""
    if frame <= keys[0][0]:
        return keys[0][1]
    if frame >= keys[-1][0]:
        return keys[-1][1]
    lo, hi = 0, len(keys) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if keys[mid][0] <= frame:
            lo = mid
        else:
            hi = mid
    (f0, v0), (f1, v1) = keys[lo], keys[hi]
    return v0 + (v1 - v0) * (frame - f0) / (f1 - f0)


def _scaled_translation(m, s):
    out = m.copy()
    out.translation = out.translation * s
    return out


def _bone_basis(bone, rel, parent_world, gm):
    """Pose-bone basis matrix for a joint whose Maya local matrix is ``rel``.

    Each bone's frame is its Maya joint frame times a quarter-turn C, so with
    parent frame C_p and own frame C:  basis = rest_rel^-1 * C_p^-1 * L * C,
    where rest_rel is the bone's rest matrix relative to its parent bone."""
    s = gm.to_scale().x
    C = bone_axis_matrix(bone.get('maya_axis', '+Y'))
    if bone.parent is not None:
        Cp = bone_axis_matrix(bone.parent.get('maya_axis', '+Y'))
        rest_rel = bone.parent.matrix_local.inverted() @ bone.matrix_local
        return rest_rel.inverted() @ Cp.inverted() @ _scaled_translation(rel, s) @ C
    world = _ortho(gm @ (parent_world @ rel)) @ C
    return bone.matrix_local.inverted() @ world


def _key_pose(target, matrix, frame, prev):
    """Key ``matrix`` on a pose bone or object, keeping its quaternion on the
    same side as the previous key so the rotation does not flip between keys.
    Returns the quaternion for the next call."""
    loc, rot, scale = matrix.decompose()
    if prev is not None and rot.dot(prev) < 0:
        rot.negate()
    target.location, target.rotation_quaternion, target.scale = loc, rot, scale
    for path in ('location', 'rotation_quaternion', 'scale'):
        target.keyframe_insert(path, frame=frame)
    return rot


def _apply_animation(data, node_world, gm, arm_obj, objects, mesh_world, log):
    """Key time-based animCurves onto pose bones and objects.

    Channels are not independent once Maya's joint orient, rotate order and the
    Y-up to Z-up swap are folded in, so every animated node is evaluated at the
    union of its key frames as a full Maya local matrix and converted to a
    Blender basis matrix.  Curves are sampled linearly between keys; Blender's
    own interpolation then smooths the result.  A node that cannot be keyed
    (a zero scale makes its matrix singular) is skipped with a warning and
    does not stop the others.  Returns the keyed frame span."""
    anim = data.animations()
    if not anim:
        return None
    scene = data.scene
    bones = arm_obj.data.bones if arm_obj else {}
    frames_seen = []

    def key_node(name, chans, node, times):
        dag_parent = scene.nodes.get(node.parent) if node.parent else None
        parent_world = node_world.get(node.parent, Matrix.Identity(4))

        def local_at(f):
            return _node_local(node, scene,
                               {a: _sample(k, f) for a, k in chans.items()})

        bone = bones.get(bl_name(name)) if arm_obj else None
        prev = None
        if bone is not None:
            target = arm_obj.pose.bones[bone.name]
            bp = bone.parent
            # a group transform between two joints still contributes its matrix
            direct = (bp is None or (dag_parent is not None
                                     and dag_parent.name == bp.name))
            bp_inv = None if direct else \
                node_world.get(bp.name, Matrix.Identity(4)).inverted()
            target.rotation_mode = 'QUATERNION'
            for f in times:
                local = local_at(f)
                rel = local if direct else bp_inv @ parent_world @ local
                B = _bone_basis(bone, rel, parent_world if bp is None else None,
                                gm)
                prev = _key_pose(target, B, f, prev)
            return

        obj = objects.get(name)
        if obj is None or obj.parent_type == 'BONE':
            return
        rest = mesh_world.get(name)               # baked meshes keep their rest
        rest_inv = _convert(gm, rest).inverted() if rest is not None else None
        obj.rotation_mode = 'QUATERNION'
        for f in times:
            m = _convert(gm, parent_world @ local_at(f))
            prev = _key_pose(obj, m if rest_inv is None else m @ rest_inv, f,
                             prev)

    for name, chans in anim.items():
        node = scene.nodes.get(name)
        if node is None:
            continue
        times = sorted({f for keys in chans.values() for f, _ in keys})
        try:
            key_node(name, chans, node, times)
        except ValueError as exc:
            log('WARNING', "animation of '%s' skipped: %s" % (name, exc))
            continue
        frames_seen.extend((times[0], times[-1]))
    return (min(frames_seen), max(frames_seen)) if frames_seen else None
