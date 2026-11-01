# SPDX-License-Identifier: GPL-3.0-or-later
"""
Native Maya ASCII (.ma) exporter (Blender -> Maya), no Maya required.

Writes a valid Maya 2023 ASCII scene from the current Blender selection /
scene: polygon meshes (vertices, polygons, all UV sets, per-vertex/-corner
normals, edge hardness, vertex colours, per-face material assignment), the
object hierarchy and visibility, materials with textures, the armature as a
joint skeleton, skin weights as a skinCluster, and shape keys as a blendShape.

This is the inverse of :mod:`ma_parser` / :mod:`scene_builder`, and goes beyond
mesh-only Blender->Maya exporters by also carrying the skeleton, skin and blend
shapes.
"""

import contextlib
import os
import re
import math

import bpy
from mathutils import Matrix, Vector


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


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------
def export(filepath, opts, context, report=None):
    def log(level, msg):
        if report:
            report({level}, msg)
        print("[Maya .ma export] %s" % msg)

    em = _export_matrix(getattr(opts, 'export_scale', 100.0),
                        getattr(opts, 'apply_axis', True))
    em3 = em.to_3x3()

    if getattr(opts, 'use_selection', False):
        objects = [o for o in context.selected_objects]
    else:
        objects = [o for o in context.scene.objects]
    meshes = [o for o in objects if o.type == 'MESH']
    armatures = [o for o in objects if o.type == 'ARMATURE']

    out = []
    w = out.append
    w("//Maya ASCII 2023 scene\n")
    w("//Name: %s\n" % os.path.basename(filepath))
    w('requires maya "2023";\n')
    w("currentUnit -l centimeter -a degree -t film;\n")
    w('fileInfo "application" "blender";\n')
    w('fileInfo "exportedFrom" "Maya Scene Importer for Blender";\n')

    used_names = set()

    def uniq(name):
        n = _san(name)
        base = n
        i = 1
        while n in used_names:
            n = "%s%d" % (base, i)
            i += 1
        used_names.add(n)
        return n

    stats = dict(meshes=0, joints=0, skins=0, shapes=0, materials=0)

    # -- materials --------------------------------------------------------
    mat_nodes = {}        # bpy material -> (shaderName, sgName)
    if getattr(opts, 'export_materials', True):
        for mat in _collect_materials(meshes):
            sh, sg = _emit_material(w, mat, uniq, filepath, em3)
            mat_nodes[mat] = (sh, sg)
            stats['materials'] += 1

    # -- armature / joints ------------------------------------------------
    bone_node = {}        # (armature, bone_name) -> joint node name
    for arm in armatures:
        _emit_skeleton(w, arm, uniq, bone_node, em)
        stats['joints'] += len(arm.data.bones)

    # -- meshes -----------------------------------------------------------
    sg_members = {}       # sgName -> [meshShape names]
    for obj in meshes:
        info = _emit_mesh(w, obj, uniq, em, em3, mat_nodes, sg_members,
                          getattr(opts, 'export_normals', True),
                          getattr(opts, 'export_uvs', True),
                          getattr(opts, 'export_colors', True))
        if info is None:
            continue
        stats['meshes'] += 1
        if info['nverts'] != len(obj.data.vertices):
            # modifiers such as subdivision renumber the vertices, so weights
            # and shape key offsets (indexed on the original) would land wrong
            if obj.vertex_groups or obj.data.shape_keys:
                log('WARNING', "'%s': its modifiers change the vertex count, so "
                    "skin weights and shape keys were not exported" % obj.name)
            continue
        # skin
        if getattr(opts, 'export_skin', True):
            if _emit_skin(w, obj, info, bone_node, uniq, em):
                stats['skins'] += 1
        # blend shapes
        if getattr(opts, 'export_shapekeys', True):
            stats['shapes'] += _emit_blendshape(w, obj, info, uniq, em3)

    # -- connections (materials -> shading engines -> shapes) -------------
    _emit_material_connections(w, mat_nodes, sg_members)

    with open(filepath, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write("".join(out))

    log('INFO', "exported %d meshes, %d joints, %d skins, %d blendShapes, %d "
        "materials -> %s" % (stats['meshes'], stats['joints'], stats['skins'],
                             stats['shapes'], stats['materials'],
                             os.path.basename(filepath)))
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


def _emit_mesh(w, obj, uniq, em, em3, mat_nodes, sg_members,
               want_normals, want_uvs, want_colors):
    with _rest_state(obj):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph) if obj.modifiers else obj
        mesh = eval_obj.to_mesh()
    if mesh is None or len(mesh.vertices) == 0:
        return None
    mesh.calc_loop_triangles()

    tname = uniq(obj.name)
    sname = uniq(obj.name + "Shape")
    # Bake world placement into the geometry in Maya space and leave the
    # transform at identity — this matches how the importer round-trips
    # (objects at identity, world baked into the vertices) and stays valid in
    # Maya.  ``full`` maps Blender-local coordinates straight to Maya world.
    full = em @ obj.matrix_world
    full3 = full.to_3x3()
    flip = full3.determinant() < 0

    w('createNode transform -n "%s";\n' % tname)
    if obj.hide_render or obj.hide_get():
        w('\tsetAttr ".v" no;\n')

    w('createNode mesh -n "%s" -p "%s";\n' % (sname, tname))
    w('\tsetAttr -k off ".v";\n')
    w('\tsetAttr ".vir" yes;\n\tsetAttr ".vif" yes;\n')

    # vertices, converted into Maya world space
    verts = mesh.vertices
    nv = len(verts)
    vchunks = []
    for v in verts:
        co = full @ v.co
        vchunks.append(" ".join(_f(c) for c in co))
    w('\tsetAttr -s %d ".vt[0:%d]" %s;\n' % (nv, nv - 1, "  ".join(vchunks)))

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
        w('\tsetAttr -s %d ".ed[0:%d]" %s;\n'
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
        w('\tsetAttr ".uvst[%d].uvsn" -type "string" "%s";\n'
          % (si, _quoted(layer.name)))
        if uvsp:
            flat = " ".join("%s %s" % (_f(u), _f(v)) for u, v in uvsp)
            w('\tsetAttr ".uvst[%d].uvsp[0:%d]" -type "float2" %s;\n'
              % (si, len(uvsp) - 1, flat))
    if uv_sets:
        active = mesh.uv_layers.active or uv_sets[0][1]
        w('\tsetAttr ".cuvs" -type "string" "%s";\n' % _quoted(active.name))

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
        w('\tsetAttr -s %d ".fc[0:%d]" -type "polyFaces"\n%s\t\t;\n'
          % (np, np - 1, "".join(frecs)))

    _emit_vertex_colors(w, color_sets, corner_order, mesh)

    # custom normals
    if want_normals:
        _emit_normals(w, mesh, full3, flip)

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

    eval_obj.to_mesh_clear()
    return dict(shape=sname, transform=tname, nverts=nv)


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
def _joint_world(arm_world, bone, em):
    """Maya-space world matrix of the joint a bone was built from, with the
    importer's aim-axis permutation (stored on the bone) taken back out."""
    from .scene_builder import bone_axis_matrix
    tag = bone.get('maya_axis', '+Y')
    return em @ (arm_world @ bone.matrix_local @ bone_axis_matrix(tag).inverted())


def _emit_skeleton(w, arm_obj, uniq, bone_node, em):
    arm_world = arm_obj.matrix_world
    order = []
    seen = set()

    def visit(bone):
        if bone.name in seen:
            return
        if bone.parent:
            visit(bone.parent)
        seen.add(bone.name)
        order.append(bone)

    for b in arm_obj.data.bones:
        visit(b)

    for bone in order:
        jname = uniq(bone.name)
        bone_node[(arm_obj, bone.name)] = jname
        parent = bone.parent
        pj = bone_node.get((arm_obj, parent.name)) if parent else None
        if pj:
            w('createNode joint -n "%s" -p "%s";\n' % (jname, pj))
        else:
            w('createNode joint -n "%s";\n' % jname)

        # world bind matrix in Maya space
        world = _joint_world(arm_world, bone, em)
        if parent:
            pworld = _joint_world(arm_world, parent, em)
            local = pworld.inverted() @ world
        else:
            local = world
        loc, rot, _ = local.decompose()
        eul = rot.to_euler('XYZ')
        w('\tsetAttr ".t" -type "double3" %s;\n'
          % " ".join(_f(v) for v in loc))
        w('\tsetAttr ".jo" -type "double3" %s;\n'
          % " ".join(_f(math.degrees(a)) for a in eul))
        w('\tsetAttr ".bps" -type "matrix" %s;\n' % _mtx_str(world))


# ---------------------------------------------------------------------------
# skin
# ---------------------------------------------------------------------------
def _emit_skin(w, obj, info, bone_node, uniq, em):
    arm = None
    for m in obj.modifiers:
        if m.type == 'ARMATURE' and m.object:
            arm = m.object
            break
    if arm is None:
        return False
    # influence joints present both as vertex groups and as bones
    influences = []
    vg_index = {}
    for vg in obj.vertex_groups:
        key = (arm, vg.name)
        if key in bone_node:
            vg_index[vg.index] = len(influences)
            influences.append(bone_node[key])
    if not influences:
        return False

    scname = uniq(obj.name + "_skinCluster")
    w('createNode skinCluster -n "%s";\n' % scname)

    mesh = obj.data
    nv = len(mesh.vertices)
    rows = []
    for v in mesh.vertices:
        pairs = []
        for g in v.groups:
            ii = vg_index.get(g.group)
            if ii is not None and g.weight > 0:
                pairs.append((ii, g.weight))
        if not pairs:
            pairs = [(0, 1.0)]
        s = sum(p[1] for p in pairs) or 1.0
        body = " ".join("%d %s" % (i, _f(wt / s)) for i, wt in pairs)
        rows.append("%d %s" % (len(pairs), body))
    w('\tsetAttr -s %d ".wl[0:%d].w" %s;\n'
      % (nv, nv - 1, "  ".join(rows)))

    # influence (joint) connections
    for k, jname in enumerate(influences):
        w('connectAttr "%s.wm" "%s.ma[%d]";\n' % (jname, scname, k))
    # skinCluster drives the mesh; remember it for deformer chaining
    info['skin'] = scname
    w('connectAttr "%s.og[0]" "%s.i";\n' % (scname, info['shape']))
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
        count += 1
    if not count:
        return 0
    w('\tsetAttr ".aal" -type "attributeAlias" {%s};\n'
      % ", ".join(a.replace('" "', '","') for a in aliases))
    # chain the blendShape ahead of the skinCluster if the mesh is skinned,
    # otherwise drive the shape directly
    if info.get('skin'):
        w('connectAttr "%s.og[0]" "%s.ip[0].ig";\n' % (bsname, info['skin']))
    else:
        w('connectAttr "%s.og[0]" "%s.i";\n' % (bsname, info['shape']))
    return count


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
    if not mat.use_nodes:
        return None
    for n in mat.node_tree.nodes:
        if n.type == 'BSDF_PRINCIPLED':
            return n
    return None


def _emit_material(w, mat, uniq, filepath, em3):
    name = uniq(mat.name)
    sg = uniq(mat.name + "SG")
    bsdf = _principled(mat)
    color = (0.5, 0.5, 0.5)
    if bsdf:
        bc = bsdf.inputs.get('Base Color')
        if bc and not bc.is_linked:
            color = bc.default_value[:3]
    elif hasattr(mat, 'diffuse_color'):
        color = mat.diffuse_color[:3]
    w('createNode lambert -n "%s";\n' % name)
    w('\tsetAttr ".c" -type "float3" %s;\n'
      % " ".join(_f(c) for c in color))
    w('createNode shadingEngine -n "%s";\n' % sg)
    w('\tsetAttr ".ihi" 0;\n\tsetAttr ".ro" yes;\n')
    # texture (base colour image), if any
    img = _base_image(bsdf)
    if img and img.filepath:
        fnode = uniq(mat.name + "_file")
        path = bpy.path.abspath(img.filepath)
        w('createNode file -n "%s";\n' % fnode)
        w('\tsetAttr ".ftn" -type "string" "%s";\n' % path.replace("\\", "/"))
        w('connectAttr "%s.oc" "%s.c";\n' % (fnode, name))
    return name, sg


def _base_image(bsdf):
    if not bsdf:
        return None
    bc = bsdf.inputs.get('Base Color')
    if bc and bc.is_linked:
        from_node = bc.links[0].from_node
        if from_node.type == 'TEX_IMAGE':
            return from_node.image
    return None


def _emit_material_connections(w, mat_nodes, sg_members):
    for mat, (sh, sg) in mat_nodes.items():
        w('connectAttr "%s.oc" "%s.ss";\n' % (sh, sg))
    for sg, members in sg_members.items():
        for sname, og in members:
            w('connectAttr "%s.iog.og[%d]" "%s.dsm" -na;\n' % (sname, og, sg))
