# SPDX-License-Identifier: GPL-3.0-or-later
"""
Native Maya ASCII (.ma) exporter (Blender -> Maya), no Maya required.

Writes a valid Maya 2023 ASCII scene from the current Blender selection /
scene: polygon meshes (vertices, polygons, all UV sets, per-vertex/-corner
normals, edge hardness, vertex colours, per-face material assignment), the
object hierarchy and visibility, standardSurface materials with their PBR
textures, the armature as a joint skeleton, skin weights as a skinCluster,
shape keys as a blendShape, cameras, lights, and animation baked to animCurves.

This is the inverse of :mod:`ma_parser` / :mod:`scene_builder`.  Every
convention here (axis swap, unit scale, bone axis tags, light intensity) undoes
what the importer does, so a scene survives a round trip.
"""

import contextlib
import os
import re
import math

import bpy
from mathutils import Matrix, Vector


# Maya creates these with every new scene; a node of the same name in the file
# would clash with them
_MAYA_DEFAULTS = {'persp', 'perspShape', 'top', 'topShape', 'front',
                  'frontShape', 'side', 'sideShape',
                  'sceneConfigurationScriptNode'}

_TIME_UNITS = {15: 'game', 24: 'film', 25: 'pal', 30: 'ntsc', 48: 'show',
               50: 'palf', 60: 'ntscf'}

# inverse of ma_model.MayaModel._LIGHT_TYPES
_LIGHT_NODES = {'SUN': 'directionalLight', 'POINT': 'pointLight',
                'SPOT': 'spotLight', 'AREA': 'areaLight'}


# ---------------------------------------------------------------------------
# coordinate conversion (Blender Z-up -> Maya Y-up), inverse of the importer
# ---------------------------------------------------------------------------
def _export_matrix(scale, apply_axis):
    if apply_axis:
        # inverse of the importer's axis swap: (x, y, z)_bl -> (x, z, -y)_maya
        axis = Matrix(((1, 0, 0, 0),
                       (0, 0, 1, 0),
                       (0, -1, 0, 0),
                       (0, 0, 0, 1)))
    else:
        axis = Matrix.Identity(4)
    return Matrix.Scale(scale, 4) @ axis


def _to_maya(em, m):
    """Blender world matrix -> Maya world matrix, the inverse of
    scene_builder._convert: the axis swap applies to the whole matrix, the
    unit scale to positions only, so a transform keeps its own scale."""
    k = em.to_scale().x
    out = Matrix.Scale(1.0 / k, 4) @ em @ m
    out.translation = out.translation * k
    return out


def _mtx_str(m):
    """mathutils.Matrix -> Maya 16-float row-major (basis vectors as rows)."""
    # Maya stores the transpose of a column-vector matrix
    vals = []
    for col in range(4):
        for row in range(4):
            vals.append(m[row][col])
    return " ".join(_f(v) for v in vals)


def _f(v):
    """Compact float formatting matching Maya's style."""
    if v == int(v):
        return str(int(v))
    return repr(round(v, 9))


def _quoted(text):
    """Text that is safe between the double quotes of a setAttr string."""
    return text.replace("\\", "/").replace('"', "'").replace("\n", " ")


def _san(name):
    out = []
    for ch in name:
        out.append(ch if (ch.isalnum() or ch == '_') else '_')
    s = "".join(out)
    if s and s[0].isdigit():
        s = "_" + s
    return s or "node"


def _time_unit(fps):
    """Maya's currentUnit -t name for a frame rate."""
    r = int(round(fps))
    if abs(fps - r) < 1e-3:
        return _TIME_UNITS.get(r, '%dfps' % r)
    return '%sfps' % _f(round(fps, 3))


def _trs(m):
    """Maya translate (cm), rotate (degrees, xyz order) and scale of a
    Maya-space matrix."""
    loc, rot, scl = m.decompose()
    return loc, rot.to_euler('XYZ'), scl


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------
def export(filepath, opts, context, report=None):
    def log(level, msg):
        if report:
            report({level}, msg)
        print("[Maya .ma export] %s" % msg)

    scene = context.scene
    em = _export_matrix(getattr(opts, 'export_scale', 100.0),
                        getattr(opts, 'apply_axis', True))
    em3 = em.to_3x3()

    if getattr(opts, 'use_selection', False):
        objects = [o for o in context.selected_objects]
    else:
        objects = [o for o in scene.objects]
    meshes = [o for o in objects if o.type == 'MESH']
    armatures = [o for o in objects if o.type == 'ARMATURE']
    cameras = [o for o in objects if o.type == 'CAMERA'] \
        if getattr(opts, 'export_cameras', True) else []
    lights = [o for o in objects if o.type == 'LIGHT'
              and o.data.type in _LIGHT_NODES] \
        if getattr(opts, 'export_lights', True) else []
    want_skin = getattr(opts, 'export_skin', True)
    want_shapes = getattr(opts, 'export_shapekeys', True)

    fps = scene.render.fps / (scene.render.fps_base or 1.0)
    out = []
    w = out.append
    w("//Maya ASCII 2023 scene\n")
    w("//Name: %s\n" % os.path.basename(filepath))
    w('requires maya "2023";\n')
    w("currentUnit -l centimeter -a degree -t %s;\n" % _time_unit(fps))
    w('fileInfo "application" "blender";\n')
    w('fileInfo "exportedFrom" "Maya Scene Importer for Blender";\n')

    used_names = set(_MAYA_DEFAULTS)

    def uniq(name):
        n = _san(name)
        base = n
        i = 1
        while n in used_names:
            n = "%s%d" % (base, i)
            i += 1
        used_names.add(n)
        return n

    stats = dict(meshes=0, joints=0, skins=0, shapes=0, materials=0,
                 cameras=0, lights=0, curves=0)

    # -- materials --------------------------------------------------------
    mat_nodes = {}        # bpy material -> (shaderName, sgName)
    if getattr(opts, 'export_materials', True):
        for mat in _collect_materials(meshes):
            sh, sg = _emit_material(w, mat, uniq)
            mat_nodes[mat] = (sh, sg)
            stats['materials'] += 1

    # -- armature / joints ------------------------------------------------
    skel = {}             # (armature, bone_name) -> dict(node, bind, jo)
    for arm in armatures:
        _emit_skeleton(w, arm, uniq, skel, em)
        stats['joints'] += len(arm.data.bones)

    # -- meshes -----------------------------------------------------------
    sg_members = {}       # sgName -> [meshShape names]
    mesh_infos = []
    for obj in meshes:
        wants = ((want_skin and _skin_influences(obj, skel) is not None) or
                 (want_shapes and obj.data.shape_keys is not None and
                  len(obj.data.shape_keys.key_blocks) > 1))
        info = _emit_mesh(w, obj, uniq, em, mat_nodes, sg_members,
                          getattr(opts, 'export_normals', True),
                          getattr(opts, 'export_uvs', True),
                          getattr(opts, 'export_colors', True), wants)
        if info is None:
            continue
        stats['meshes'] += 1
        info['obj'] = obj
        info['rest'] = obj.matrix_world.copy()
        mesh_infos.append(info)
        if wants and not info['orig']:
            # modifiers such as subdivision renumber the vertices, so weights
            # and shape key offsets (indexed on the original) would land wrong
            log('WARNING', "'%s': its modifiers change the vertex count, so "
                "skin weights and shape keys were not exported" % obj.name)
            continue
        if want_shapes:
            stats['shapes'] += _emit_blendshape(w, obj, info, uniq, em3)
        if want_skin and _emit_skin(w, obj, info, skel, uniq):
            stats['skins'] += 1
        _emit_deformer_chain(w, info)

    # -- cameras and lights -----------------------------------------------
    xform_infos = []
    for obj in cameras:
        xform_infos.append(_emit_camera(w, obj, uniq, em, scene))
        stats['cameras'] += 1
    for obj in lights:
        xform_infos.append(_emit_light(w, obj, uniq, em))
        stats['lights'] += 1

    # -- connections (materials -> shading engines -> shapes) -------------
    _emit_material_connections(w, mat_nodes, sg_members)

    # -- animation --------------------------------------------------------
    if getattr(opts, 'export_animation', True):
        stats['curves'] = _emit_animation(w, scene, uniq, em, mesh_infos,
                                          xform_infos, armatures, skel, log)
    _emit_play_range(w, scene)

    with open(filepath, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write("".join(out))

    log('INFO', "exported %d meshes, %d joints, %d skins, %d blendShapes, %d "
        "materials, %d cameras, %d lights, %d animCurves -> %s"
        % (stats['meshes'], stats['joints'], stats['skins'], stats['shapes'],
           stats['materials'], stats['cameras'], stats['lights'],
           stats['curves'], os.path.basename(filepath)))
    return stats


# ---------------------------------------------------------------------------
# meshes
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _rest_state(obj):
    """Evaluate the object un-posed and on its basis shape.  Skin weights and
    blend shape deltas are exported separately, so baking the current pose or
    shape key mix into the vertices would deform the mesh twice in Maya."""
    arm_mods = [(m, m.show_viewport) for m in obj.modifiers
                if m.type == 'ARMATURE']
    keys = obj.data.shape_keys
    saved_sk = (obj.show_only_shape_key, obj.active_shape_key_index) \
        if keys else None
    for m, _ in arm_mods:
        m.show_viewport = False
    if keys:
        obj.show_only_shape_key = True
        obj.active_shape_key_index = 0
    bpy.context.view_layer.update()
    try:
        yield
    finally:
        for m, shown in arm_mods:
            m.show_viewport = shown
        if saved_sk:
            obj.show_only_shape_key, obj.active_shape_key_index = saved_sk


def _ranges(indices):
    """[1, 2, 3, 7] -> ['f[1:3]', 'f[7]'], Maya's component list syntax."""
    out = []
    start = prev = None
    for i in indices:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            out.append((start, prev))
            start = prev = i
    if start is not None:
        out.append((start, prev))
    return ['f[%d]' % a if a == b else 'f[%d:%d]' % (a, b) for a, b in out]


def _emit_mesh(w, obj, uniq, em, mat_nodes, sg_members,
               want_normals, want_uvs, want_colors, deformed):
    """Write a mesh.  A ``deformed`` mesh (skin or blend shapes) gets its
    geometry on an intermediate ``Orig`` shape that feeds the deformer chain,
    the way Maya stores it; the visible shape takes the chain's output."""
    with _rest_state(obj):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph) if obj.modifiers else obj
        mesh = eval_obj.to_mesh()
    if mesh is None or len(mesh.vertices) == 0:
        return None
    mesh.calc_loop_triangles()
    deformed = deformed and len(mesh.vertices) == len(obj.data.vertices)

    tname = uniq(obj.name)
    sname = uniq(obj.name + "Shape")
    oname = uniq(obj.name + "ShapeOrig") if deformed else None
    # Bake world placement into the geometry in Maya space and leave the
    # transform at identity — this matches how the importer round-trips
    # (objects at identity, world baked into the vertices) and stays valid in
    # Maya.  ``full`` maps Blender-local coordinates straight to Maya world.
    full = em @ obj.matrix_world
    full3 = full.to_3x3()
    flip = full3.determinant() < 0

    geo = []              # geometry setAttrs, for whichever shape holds them
    g = geo.append

    # vertices, converted into Maya world space
    verts = mesh.vertices
    nv = len(verts)
    vchunks = []
    for v in verts:
        co = full @ v.co
        vchunks.append(" ".join(_f(c) for c in co))
    g('\tsetAttr -s %d ".vt[0:%d]" %s;\n' % (nv, nv - 1, "  ".join(vchunks)))

    # edges (with hardness: Maya 0 == hard/sharp, 1 == soft)
    edges = mesh.edges
    ne = len(edges)
    echunks = []
    edge_index = {}
    for i, e in enumerate(edges):
        a, b = e.vertices[0], e.vertices[1]
        edge_index[frozenset((a, b))] = (i, a, b)
        hard = 0 if e.use_edge_sharp else 1
        echunks.append("%d %d %d" % (a, b, hard))
    if ne:
        g('\tsetAttr -s %d ".ed[0:%d]" %s;\n'
          % (ne, ne - 1, "  ".join(echunks)))

    # uv sets: every layer, under its own name
    uv_sets = []                      # (set index, layer, {uv: id})
    for layer in (mesh.uv_layers if want_uvs else ()):
        uv_lookup, uvsp = {}, []
        for loop_i in range(len(mesh.loops)):
            uv = layer.data[loop_i].uv
            key = (round(uv.x, 6), round(uv.y, 6))
            if key not in uv_lookup:
                uv_lookup[key] = len(uvsp)
                uvsp.append(key)
        si = len(uv_sets)
        uv_sets.append((si, layer, uv_lookup))
        g('\tsetAttr ".uvst[%d].uvsn" -type "string" "%s";\n'
          % (si, _quoted(layer.name)))
        if uvsp:
            flat = " ".join("%s %s" % (_f(u), _f(v)) for u, v in uvsp)
            g('\tsetAttr ".uvst[%d].uvsp[0:%d]" -type "float2" %s;\n'
              % (si, len(uvsp) - 1, flat))
    if uv_sets:
        active = mesh.uv_layers.active or uv_sets[0][1]
        g('\tsetAttr ".cuvs" -type "string" "%s";\n' % _quoted(active.name))

    # polyFaces: f (edge ids, signed) + mu (uv ids) + mc (colour ids)
    polys = mesh.polygons
    np = len(polys)
    frecs = []
    color_sets = _color_sets(mesh) if want_colors else []
    corner_order = []                 # loop index of every corner, as written
    for poly in polys:
        vs = list(poly.vertices)
        n = len(vs)
        loops = list(range(poly.loop_start, poly.loop_start + n))
        if flip:                      # a mirroring transform inverts the winding
            vs.reverse()
            loops.reverse()
        edge_ids = []
        for k in range(n):
            u, ww = vs[k], vs[(k + 1) % n]
            ei = edge_index.get(frozenset((u, ww)))
            if ei is None:
                edge_ids.append(0)
                continue
            idx, a, b = ei
            edge_ids.append(idx if a == u else (-idx - 1))
        rec = "\t\tf %d %s\n" % (n, " ".join(str(e) for e in edge_ids))
        for ci in range(len(color_sets)):
            base = len(corner_order)
            rec += "\t\tmc %d %d %s\n" % (
                ci, n, " ".join(str(base + k) for k in range(n)))
        corner_order.extend(loops)
        for si, layer, uv_lookup in uv_sets:
            uv_ids = []
            for loop_i in loops:
                uv = layer.data[loop_i].uv
                uv_ids.append(uv_lookup[(round(uv.x, 6), round(uv.y, 6))])
            rec += "\t\tmu %d %d %s\n" % (si, n, " ".join(str(u) for u in uv_ids))
        frecs.append(rec)
    if np:
        g('\tsetAttr -s %d ".fc[0:%d]" -type "polyFaces"\n%s\t\t;\n'
          % (np, np - 1, "".join(frecs)))

    _emit_vertex_colors(g, color_sets, corner_order, mesh)

    # custom normals
    if want_normals:
        _emit_normals(g, mesh, full3, flip)

    # -- write the transform and shape(s) ----------------------------------
    w('createNode transform -n "%s";\n' % tname)
    if obj.hide_render or obj.hide_get():
        w('\tsetAttr ".v" no;\n')

    w('createNode mesh -n "%s" -p "%s";\n' % (sname, tname))
    w('\tsetAttr -k off ".v";\n')
    w('\tsetAttr ".vir" yes;\n\tsetAttr ".vif" yes;\n')
    if not deformed:
        out_geo = geo
    else:
        out_geo = None

    # material assignment -> remember membership for connectAttr pass
    mat_slots = obj.data.materials
    if mat_slots:
        # group polygons by material index
        by_mat = {}
        for poly in polys:
            by_mat.setdefault(poly.material_index, []).append(poly.index)
        w('\tsetAttr -s %d ".iog[0].og";\n' % max(1, len(by_mat)))
        for og, (mi, faces) in enumerate(sorted(by_mat.items())):
            mat = mat_slots[mi] if mi < len(mat_slots) else None
            if mat is None or mat not in mat_nodes:
                continue
            sg = mat_nodes[mat][1]
            sg_members.setdefault(sg, []).append((sname, og))
            if len(by_mat) > 1:       # per-face assignment, e.g. a glass visor
                comps = _ranges(faces)
                w('\tsetAttr ".iog[0].og[%d].gcl" -type "componentList" %d %s;\n'
                  % (og, len(comps), " ".join('"%s"' % c for c in comps)))
    if out_geo:
        w("".join(out_geo))
    if deformed:
        w('createNode mesh -n "%s" -p "%s";\n' % (oname, tname))
        w('\tsetAttr -k off ".v";\n\tsetAttr ".io" yes;\n')
        w("".join(geo))

    eval_obj.to_mesh_clear()
    return dict(shape=sname, transform=tname, orig=oname, nverts=nv)


def _color_sets(mesh):
    """[(name, [rgba per face corner])] for every colour attribute."""
    sets = []
    for attr in mesh.color_attributes:
        flat = [0.0] * (len(attr.data) * 4)
        attr.data.foreach_get('color', flat)
        rgba = [tuple(flat[i:i + 4]) for i in range(0, len(flat), 4)]
        if attr.domain == 'POINT':
            rgba = [rgba[loop.vertex_index] for loop in mesh.loops]
        sets.append((attr.name, rgba))
    return sets


def _emit_vertex_colors(w, color_sets, corner_order, mesh):
    """Colour sets with one colour per face corner, listed in the order the
    corners were written so the ``mc`` ids of each face are consecutive."""
    if not color_sets or not corner_order:
        return
    active = mesh.color_attributes.active_color
    w('\tsetAttr ".ccls" -type "string" "%s";\n'
      % _quoted(active.name if active else color_sets[0][0]))
    for ci, (name, rgba) in enumerate(color_sets):
        w('\tsetAttr ".clst[%d].clsn" -type "string" "%s";\n'
          % (ci, _quoted(name)))
        flat = " ".join("%s %s %s %s" % tuple(_f(c) for c in rgba[loop_i])
                        for loop_i in corner_order)
        w('\tsetAttr -s %d ".clst[%d].clsp[0:%d]" -type "float4" %s;\n'
          % (len(corner_order), ci, len(corner_order) - 1, flat))


def _emit_normals(w, mesh, full3, flip):
    """Per face-corner normals in Maya world space (the vertices are baked
    there), in the same corner order as the polygons are written."""
    try:
        nm = full3.inverted().transposed()
    except ValueError:                # a zero scale has no normals worth writing
        return
    chunks = []
    for poly in mesh.polygons:
        corners = range(poly.loop_start, poly.loop_start + poly.loop_total)
        for loop_i in (reversed(corners) if flip else corners):
            nrm = (nm @ mesh.loops[loop_i].normal).normalized()
            chunks.append(" ".join(_f(c) for c in nrm))
    if chunks:
        w('\tsetAttr -s %d ".n[0:%d]" -type "float3" %s;\n'
          % (len(chunks), len(chunks) - 1, "  ".join(chunks)))


# ---------------------------------------------------------------------------
# skeleton
# ---------------------------------------------------------------------------
def _joint_world(arm_world, bone, em, matrix=None):
    """Maya world matrix of the joint a bone was built from, with the
    importer's aim-axis permutation (stored on the bone) taken back out.
    ``matrix`` is a posed armature-space matrix; the rest pose by default."""
    from .scene_builder import bone_axis_matrix
    tag = bone.get('maya_axis', '+Y')
    m = bone.matrix_local if matrix is None else matrix
    return _to_maya(em, arm_world @ m @ bone_axis_matrix(tag).inverted())


def _bone_order(arm_obj):
    order, seen = [], set()

    def visit(bone):
        if bone.name in seen:
            return
        if bone.parent:
            visit(bone.parent)
        seen.add(bone.name)
        order.append(bone)

    for b in arm_obj.data.bones:
        visit(b)
    return order


def _emit_skeleton(w, arm_obj, uniq, skel, em):
    arm_world = arm_obj.matrix_world
    for bone in _bone_order(arm_obj):
        jname = uniq(bone.name)
        parent = bone.parent
        pj = skel.get((arm_obj, parent.name)) if parent else None
        if pj:
            w('createNode joint -n "%s" -p "%s";\n' % (jname, pj['node']))
        else:
            w('createNode joint -n "%s";\n' % jname)

        # world bind matrix in Maya space; the joint's rest rotation goes
        # into its joint orient, leaving rotate free for animation
        world = _joint_world(arm_world, bone, em)
        local = pj['bind'].inverted() @ world if pj else world
        loc, rot, _ = local.decompose()
        eul = rot.to_euler('XYZ')
        w('\tsetAttr ".t" -type "double3" %s;\n'
          % " ".join(_f(v) for v in loc))
        w('\tsetAttr ".jo" -type "double3" %s;\n'
          % " ".join(_f(math.degrees(a)) for a in eul))
        w('\tsetAttr ".bps" -type "matrix" %s;\n' % _mtx_str(world))
        skel[(arm_obj, bone.name)] = dict(node=jname, bind=world,
                                          jo=eul.to_matrix().to_4x4())


# ---------------------------------------------------------------------------
# skin
# ---------------------------------------------------------------------------
def _skin_influences(obj, skel):
    """(armature, [(vertex group index, joint key)]) or None."""
    arm = None
    for m in obj.modifiers:
        if m.type == 'ARMATURE' and m.object:
            arm = m.object
            break
    if arm is None:
        return None
    pairs = [(vg.index, (arm, vg.name)) for vg in obj.vertex_groups
             if (arm, vg.name) in skel]
    return (arm, pairs) if pairs else None


def _emit_skin(w, obj, info, skel, uniq):
    found = _skin_influences(obj, skel)
    if found is None:
        return False
    _, pairs = found
    vg_index = {gi: k for k, (gi, _) in enumerate(pairs)}
    keys = [key for _, key in pairs]

    scname = uniq(obj.name + "_skinCluster")
    w('createNode skinCluster -n "%s";\n' % scname)

    mesh = obj.data
    nv = len(mesh.vertices)
    rows = []
    for v in mesh.vertices:
        wts = []
        for gr in v.groups:
            ii = vg_index.get(gr.group)
            if ii is not None and gr.weight > 0:
                wts.append((ii, gr.weight))
        if not wts:
            wts = [(0, 1.0)]
        s = sum(p[1] for p in wts) or 1.0
        body = " ".join("%d %s" % (i, _f(wt / s)) for i, wt in wts)
        rows.append("%d %s" % (len(wts), body))
    w('\tsetAttr -s %d ".wl[0:%d].w" %s;\n'
      % (nv, nv - 1, "  ".join(rows)))
    # bind pre-matrices: each influence's inverse world matrix at bind time.
    # The geometry is baked in world space, so the geometry matrix stays
    # identity.
    for k, key in enumerate(keys):
        w('\tsetAttr ".pm[%d]" -type "matrix" %s;\n'
          % (k, _mtx_str(skel[key]['bind'].inverted())))
    w('\tsetAttr ".mi" %d;\n' % max(len(r.split()) // 2 for r in rows))

    for k, key in enumerate(keys):
        w('connectAttr "%s.wm" "%s.ma[%d]";\n' % (skel[key]['node'], scname, k))
    info['skin'] = scname
    return True


# ---------------------------------------------------------------------------
# blend shapes
# ---------------------------------------------------------------------------
def _emit_blendshape(w, obj, info, uniq, em3):
    mesh = obj.data
    keys = mesh.shape_keys
    if not keys or len(keys.key_blocks) < 2:
        return 0
    basis = keys.key_blocks[0]
    bsname = uniq(obj.name + "_blendShape")
    w('createNode blendShape -n "%s";\n' % bsname)
    count = 0
    aliases = []
    targets = []
    for ti, kb in enumerate(keys.key_blocks[1:]):
        deltas = []
        for vi, (b, p) in enumerate(zip(basis.data, kb.data)):
            d = p.co - b.co
            if d.length > 1e-6:
                md = em3 @ d
                deltas.append((vi, md))
        if not deltas:
            continue
        pts = " ".join("%s %s %s 1" % (_f(d.x), _f(d.y), _f(d.z))
                       for _, d in deltas)
        comp = " ".join('"vtx[%d]"' % vi for vi, _ in deltas)
        w('\tsetAttr ".it[0].itg[%d].iti[6000].ipt" -type "pointArray" %d %s;\n'
          % (ti, len(deltas), pts))
        w('\tsetAttr ".it[0].itg[%d].iti[6000].ict" -type "componentList" %d %s;\n'
          % (ti, len(deltas), comp))
        aliases.append('"%s" "weight[%d]"' % (re.sub(r'\W', '_', kb.name), ti))
        if kb.value:
            w('\tsetAttr ".w[%d]" %s;\n' % (ti, _f(kb.value)))
        targets.append((ti, kb))
        count += 1
    if not count:
        return 0
    w('\tsetAttr ".aal" -type "attributeAlias" {%s};\n'
      % ", ".join(a.replace('" "', '","') for a in aliases))
    info['blendshape'] = (bsname, targets)
    return count


def _emit_deformer_chain(w, info):
    """Orig shape -> blendShape -> skinCluster -> visible shape, Maya's
    default deformer order."""
    if not info.get('orig'):
        return
    src = '%s.w' % info['orig']
    for node in ((info.get('blendshape') or (None,))[0], info.get('skin')):
        if node:
            w('connectAttr "%s" "%s.ip[0].ig";\n' % (src, node))
            src = '%s.og[0]' % node
    w('connectAttr "%s" "%s.i";\n' % (src, info['shape']))


# ---------------------------------------------------------------------------
# cameras and lights
# ---------------------------------------------------------------------------
def _emit_xform(w, name, mm, hidden):
    loc, eul, scl = _trs(mm)
    w('createNode transform -n "%s";\n' % name)
    w('\tsetAttr ".t" -type "double3" %s;\n' % " ".join(_f(v) for v in loc))
    w('\tsetAttr ".r" -type "double3" %s;\n'
      % " ".join(_f(math.degrees(a)) for a in eul))
    w('\tsetAttr ".s" -type "double3" %s;\n' % " ".join(_f(v) for v in scl))
    if hidden:
        w('\tsetAttr ".v" no;\n')


def _emit_camera(w, obj, uniq, em, scene):
    cam = obj.data
    k = em.to_scale().x
    tname = uniq(obj.name)
    sname = uniq(obj.name + "Shape")
    _emit_xform(w, tname, _to_maya(em, obj.matrix_world),
                obj.hide_render or obj.hide_get())
    w('createNode camera -n "%s" -p "%s";\n' % (sname, tname))
    w('\tsetAttr -k off ".v";\n')
    # film back in inches; Blender's sensor fits one side, the render
    # aspect gives the other
    rx = scene.render.resolution_x * scene.render.pixel_aspect_x
    ry = scene.render.resolution_y * scene.render.pixel_aspect_y
    aspect = (rx / ry) if ry else 1.0
    if cam.sensor_fit == 'VERTICAL':
        hor, ver, fit = cam.sensor_height * aspect, cam.sensor_height, 2
    else:
        hor, ver, fit = cam.sensor_width, cam.sensor_width / aspect, 1
    w('\tsetAttr ".cap" -type "double2" %s %s;\n'
      % (_f(hor / 25.4), _f(ver / 25.4)))
    w('\tsetAttr ".ff" %d;\n' % fit)
    w('\tsetAttr ".fl" %s;\n' % _f(cam.lens))
    w('\tsetAttr ".ncp" %s;\n' % _f(cam.clip_start * k))
    w('\tsetAttr ".fcp" %s;\n' % _f(cam.clip_end * k))
    if cam.type == 'ORTHO':
        w('\tsetAttr ".o" yes;\n')
        w('\tsetAttr ".ow" %s;\n' % _f(cam.ortho_scale * k))
    return dict(obj=obj, transform=tname, shape=sname)


def _emit_light(w, obj, uniq, em):
    lt = obj.data
    tname = uniq(obj.name)
    sname = uniq(obj.name + "Shape")
    _emit_xform(w, tname, _to_maya(em, obj.matrix_world),
                obj.hide_render or obj.hide_get())
    w('createNode %s -n "%s" -p "%s";\n' % (_LIGHT_NODES[lt.type], sname, tname))
    w('\tsetAttr -k off ".v";\n')
    w('\tsetAttr ".cl" -type "float3" %s;\n'
      % " ".join(_f(c) for c in lt.color[:3]))
    # the importer turns Maya intensity into watts by * 1000, except for suns
    w('\tsetAttr ".in" %s;\n'
      % _f(lt.energy if lt.type == 'SUN' else lt.energy / 1000.0))
    if lt.type == 'SPOT':
        w('\tsetAttr ".ca" %s;\n' % _f(math.degrees(lt.spot_size)))
    return dict(obj=obj, transform=tname, shape=sname)


# ---------------------------------------------------------------------------
# animation
# ---------------------------------------------------------------------------
def _animated(objs):
    """True when anything the export samples has animation or drivers."""
    seen = set()
    for o in objs:
        while o is not None and o not in seen:
            seen.add(o)
            ids = [o, o.data]
            if o.type == 'MESH' and o.data.shape_keys:
                ids.append(o.data.shape_keys)
            if any(getattr(i, 'animation_data', None) for i in ids):
                return True
            for c in getattr(o, 'constraints', ()):
                if getattr(c, 'target', None) is not None:
                    return True
            o = o.parent
    return False


def _emit_animation(w, scene, uniq, em, mesh_infos, xform_infos, armatures,
                    skel, log):
    """Bake the scene range to one key per frame.  Maya's channels are not
    independent of Blender's once the axis swap, joint orients and bone axis
    tags are folded in, so every node is sampled as a full matrix and split
    back into Maya's translate / rotate / scale.  A node whose matrix cannot
    be inverted on some frame (a zero scale) is skipped with a warning, like
    the importer does.  Returns the curve count."""
    objs = [i['obj'] for i in mesh_infos + xform_infos] + list(armatures)
    if not _animated(objs):
        return 0
    em_inv = em.inverted()
    frames = list(range(scene.frame_start, scene.frame_end + 1))
    trs = {}              # Maya node -> list of (t, euler, s) per frame
    scalars = {}          # (Maya node, attr) -> list of values per frame
    prev = {}             # Maya node -> previous euler, for continuity
    bad = {}              # Maya node -> Blender name, for nodes that failed

    def inv(m, node, name):
        try:
            return m.inverted()
        except ValueError:
            bad[node] = name
            return Matrix.Identity(4)

    def add_trs(node, mm):
        loc, rot, scl = mm.decompose()
        eul = rot.to_euler('XYZ', prev[node]) if node in prev \
            else rot.to_euler('XYZ')
        prev[node] = eul
        trs.setdefault(node, []).append((loc, eul, scl))

    saved = (scene.frame_current, scene.frame_subframe)
    try:
        for f in frames:
            scene.frame_set(f)
            for info in mesh_infos:
                obj = info['obj']
                if not info.get('skin'):
                    # the vertices are baked at the rest world matrix, so the
                    # transform carries the motion relative to it
                    add_trs(info['transform'], em @ obj.matrix_world
                            @ inv(info['rest'], info['transform'], obj.name)
                            @ em_inv)
                bs = info.get('blendshape')
                if bs:
                    for ti, kb in bs[1]:
                        scalars.setdefault((bs[0], 'w[%d]' % ti), []).append(
                            kb.value)
            for info in xform_infos:
                obj = info['obj']
                add_trs(info['transform'], _to_maya(em, obj.matrix_world))
                if obj.type == 'CAMERA':
                    scalars.setdefault((info['shape'], 'fl'), []).append(
                        obj.data.lens)
            for arm in armatures:
                world = {}
                for bone in _bone_order(arm):
                    pb = arm.pose.bones[bone.name]
                    j = skel[(arm, bone.name)]
                    wm = _joint_world(arm.matrix_world, bone, em, pb.matrix)
                    world[bone.name] = wm
                    local = inv(world[bone.parent.name], j['node'],
                                bone.name) @ wm if bone.parent else wm
                    # rotate = joint orient^-1 * local rotation
                    loc, rot, scl = local.decompose()
                    r = j['jo'].inverted() @ rot.to_matrix().to_4x4()
                    m = Matrix.Translation(loc) @ r @ \
                        Matrix.Diagonal(scl.to_4d())
                    add_trs(j['node'], m)
    finally:
        scene.frame_set(saved[0], subframe=saved[1])

    for name in sorted(bad.values()):
        log('WARNING', "animation of '%s' skipped: it scales to zero on some "
            "frame" % name)
    count = 0
    for node, samples in trs.items():
        if node in bad:
            continue
        flat = [[s[0][i] for s in samples] for i in range(3)] + \
               [[s[1][i] for s in samples] for i in range(3)] + \
               [[s[2][i] for s in samples] for i in range(3)]
        if all(max(v) - min(v) < 1e-5 for v in flat):
            continue
        for i, attr in enumerate(('tx', 'ty', 'tz', 'rx', 'ry', 'rz',
                                  'sx', 'sy', 'sz')):
            values = flat[i]
            if 3 <= i < 6:
                ctype, values = 'animCurveTA', [math.degrees(v) for v in values]
            elif i < 3:
                ctype = 'animCurveTL'
            else:
                ctype = 'animCurveTU'
            _emit_curve(w, uniq, node, attr, ctype, frames, values)
            count += 1
    for (node, attr), values in scalars.items():
        if max(values) - min(values) < 1e-6:
            continue
        _emit_curve(w, uniq, node, attr, 'animCurveTU', frames, values)
        count += 1
    return count


def _emit_curve(w, uniq, node, attr, ctype, frames, values):
    cname = uniq('%s_%s' % (node, re.sub(r'\W', '', attr)))
    w('createNode %s -n "%s";\n' % (ctype, cname))
    w('\tsetAttr ".wgt" no;\n')
    pairs = "  ".join("%s %s" % (_f(f), _f(v)) for f, v in zip(frames, values))
    w('\tsetAttr -s %d ".ktv[0:%d]" %s;\n'
      % (len(frames), len(frames) - 1, pairs))
    w('connectAttr "%s.o" "%s.%s";\n' % (cname, node, attr))


def _emit_play_range(w, scene):
    """The playback range, the way Maya stores it."""
    w('createNode script -n "sceneConfigurationScriptNode";\n')
    w('\tsetAttr ".b" -type "string" "playbackOptions -min %d -max %d '
      '-ast %d -aet %d ";\n' % (scene.frame_start, scene.frame_end,
                                scene.frame_start, scene.frame_end))
    w('\tsetAttr ".st" 6;\n')


# ---------------------------------------------------------------------------
# materials
# ---------------------------------------------------------------------------
def _collect_materials(meshes):
    mats = []
    seen = set()
    for o in meshes:
        for m in o.data.materials:
            if m and m not in seen:
                seen.add(m)
                mats.append(m)
    return mats


def _principled(mat):
    if not getattr(mat, 'use_nodes', True) or not mat.node_tree:
        return None
    for n in mat.node_tree.nodes:
        if n.type == 'BSDF_PRINCIPLED':
            return n
    return None


_SPLIT = ('SEPARATE_COLOR', 'SEPRGB', 'SEPARATE_RGB')


def _image_source(sock):
    """(image, file output attr) feeding a socket, directly or through a
    Separate Color (glTF packs metal and roughness that way), else None."""
    if sock is None or not sock.is_linked:
        return None
    link = sock.links[0]
    node = link.from_node
    if node.type == 'TEX_IMAGE':
        if node.image is None:
            return None
        return node.image, ('oa' if link.from_socket.name == 'Alpha' else 'oc')
    if node.type in _SPLIT:
        idx = [s.identifier for s in node.outputs].index(
            link.from_socket.identifier)
        src = _image_source(node.inputs[0])
        if src:
            return src[0], ('ocr', 'ocg', 'ocb')[min(idx, 2)]
    return None


def _value(bsdf, *names, default=None):
    for n in names:
        sock = bsdf.inputs.get(n)
        if sock is not None:
            return sock.default_value
    return default


def _emit_material(w, mat, uniq):
    """A Principled BSDF as an Autodesk standardSurface: base colour,
    metalness, roughness, emission, opacity and normal or bump map, each with
    its texture when one is wired in."""
    name = uniq(mat.name)
    sg = uniq(mat.name + "SG")
    bsdf = _principled(mat)
    attrs = ['\tsetAttr ".base" 1;\n']
    links = []            # (image, file output, destination plug, raw)

    if bsdf is None:
        color = tuple(mat.diffuse_color[:3]) if hasattr(mat, 'diffuse_color') \
            else (0.5, 0.5, 0.5)
        attrs.append('\tsetAttr ".baseColor" -type "float3" %s;\n'
                     % " ".join(_f(c) for c in color))
    else:
        def plug(socket_name, dest, raw, scalar=False):
            src = _image_source(bsdf.inputs.get(socket_name))
            if src is None:
                return False
            img, out = src
            if scalar and out == 'oc':
                out = 'ocr'
            links.append((img, out, dest, raw))
            return True

        color = _value(bsdf, 'Base Color', default=(0.8, 0.8, 0.8, 1))[:3]
        attrs.append('\tsetAttr ".baseColor" -type "float3" %s;\n'
                     % " ".join(_f(c) for c in color))
        plug('Base Color', 'baseColor', False)
        attrs.append('\tsetAttr ".metalness" %s;\n'
                     % _f(_value(bsdf, 'Metallic', default=0.0)))
        plug('Metallic', 'metalness', True, scalar=True)
        attrs.append('\tsetAttr ".specularRoughness" %s;\n'
                     % _f(_value(bsdf, 'Roughness', default=0.5)))
        plug('Roughness', 'specularRoughness', True, scalar=True)

        # emission: Blender 4 splits colour and strength, 3.x has one colour
        ecolor = _value(bsdf, 'Emission Color', 'Emission',
                        default=(0, 0, 0, 1))[:3]
        strength = _value(bsdf, 'Emission Strength', default=1.0)
        textured = plug('Emission Color', 'emissionColor', False) or \
            plug('Emission', 'emissionColor', False)
        if strength and (textured or any(ecolor)):
            attrs.append('\tsetAttr ".emission" %s;\n' % _f(strength))
            attrs.append('\tsetAttr ".emissionColor" -type "float3" %s;\n'
                         % " ".join(_f(c) for c in ecolor))

        # opacity: Maya's is a colour, so an alpha map drives all three
        alpha_src = _image_source(bsdf.inputs.get('Alpha'))
        if alpha_src:
            for ch in 'RGB':
                links.append((alpha_src[0], 'oa', 'opacity' + ch, True))
        else:
            alpha = _value(bsdf, 'Alpha', default=1.0)
            if alpha < 0.999:
                attrs.append('\tsetAttr ".opacity" -type "float3" %s %s %s;\n'
                             % ((_f(alpha),) * 3))

    w('createNode standardSurface -n "%s";\n' % name)
    w("".join(attrs))
    w('createNode shadingEngine -n "%s";\n' % sg)
    w('\tsetAttr ".ihi" 0;\n\tsetAttr ".ro" yes;\n')

    files = {}            # image name -> file node

    def file_node(img, raw):
        if img.name in files:
            return files[img.name]
        path = bpy.path.abspath(img.filepath) if img.filepath else ''
        if not path:
            return None   # a generated or packed-only image has no file
        fnode = uniq("%s_%s" % (mat.name, os.path.splitext(img.name)[0]))
        p2d = uniq(fnode + "_place2d")
        w('createNode file -n "%s";\n' % fnode)
        w('\tsetAttr ".ftn" -type "string" "%s";\n' % _quoted(path))
        if raw:
            w('\tsetAttr ".cs" -type "string" "Raw";\n')
            w('\tsetAttr ".ifr" yes;\n')
        w('createNode place2dTexture -n "%s";\n' % p2d)
        w('connectAttr "%s.o" "%s.uv";\n' % (p2d, fnode))
        w('connectAttr "%s.ofs" "%s.fs";\n' % (p2d, fnode))
        files[img.name] = fnode
        return fnode

    for img, out, dest, raw in links:
        fnode = file_node(img, raw)
        if fnode:
            w('connectAttr "%s.%s" "%s.%s";\n' % (fnode, out, name, dest))

    # normal or bump map, through a bump2d as Maya wires it
    nsock = bsdf.inputs.get('Normal') if bsdf else None
    if nsock is not None and nsock.is_linked:
        node = nsock.links[0].from_node
        src = mode = None
        if node.type == 'NORMAL_MAP':
            src = _image_source(node.inputs.get('Color'))
            mode = 2 if node.space == 'OBJECT' else 1
        elif node.type == 'BUMP':
            src = _image_source(node.inputs.get('Height'))
            mode = 0
        fnode = file_node(src[0], True) if src else None
        if fnode:
            bump = uniq(mat.name + "_bump2d")
            depth = node.inputs.get('Strength')
            w('createNode bump2d -n "%s";\n' % bump)
            w('\tsetAttr ".bi" %d;\n' % mode)
            w('\tsetAttr ".bd" %s;\n'
              % _f(depth.default_value if depth is not None else 1.0))
            w('connectAttr "%s.oa" "%s.bv";\n' % (fnode, bump))
            w('connectAttr "%s.o" "%s.n";\n' % (bump, name))
    return name, sg


def _emit_material_connections(w, mat_nodes, sg_members):
    for mat, (sh, sg) in mat_nodes.items():
        w('connectAttr "%s.oc" "%s.ss";\n' % (sh, sg))
    for sg, members in sg_members.items():
        for sname, og in members:
            w('connectAttr "%s.iog.og[%d]" "%s.dsm" -na;\n' % (sname, og, sg))
