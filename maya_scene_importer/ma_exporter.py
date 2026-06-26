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

import os
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
        # skin
        if getattr(opts, 'export_skin', True):
            if _emit_skin(w, obj, info, bone_node, uniq, em):
                stats['skins'] += 1
        # blend shapes
        if getattr(opts, 'export_shapekeys', True):
            stats['shapes'] += _emit_blendshape(w, obj, info, uniq, em3)

    # -- connections (materials -> shading engines -> shapes) -------------
    _emit_material_connections(w, mat_nodes, sg_members)

    with open(filepath, 'w', encoding='utf-8') as fh:
        fh.write("".join(out))

    log('INFO', "exported %d meshes, %d joints, %d skins, %d blendShapes, %d "
        "materials -> %s" % (stats['meshes'], stats['joints'], stats['skins'],
                             stats['shapes'], stats['materials'],
                             os.path.basename(filepath)))
    return stats


# ---------------------------------------------------------------------------
# meshes
# ---------------------------------------------------------------------------
def _emit_mesh(w, obj, uniq, em, em3, mat_nodes, sg_members,
               want_normals, want_uvs, want_colors):
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

    # uv set
    uv_layer = mesh.uv_layers.active if want_uvs else None
    uvsp = []
    uv_lookup = {}
    if uv_layer:
        for loop_i in range(len(mesh.loops)):
            uv = uv_layer.data[loop_i].uv
            key = (round(uv.x, 6), round(uv.y, 6))
            if key not in uv_lookup:
                uv_lookup[key] = len(uvsp)
                uvsp.append(key)
        w('\tsetAttr ".uvst[0].uvsn" -type "string" "map1";\n')
        if uvsp:
            flat = " ".join("%s %s" % (_f(u), _f(v)) for u, v in uvsp)
            w('\tsetAttr ".uvst[0].uvsp[0:%d]" -type "float2" %s;\n'
              % (len(uvsp) - 1, flat))
        w('\tsetAttr ".cuvs" -type "string" "map1";\n')

    # polyFaces: f (edge ids, signed) + mu (uv ids)
    polys = mesh.polygons
    np = len(polys)
    frecs = []
    for poly in polys:
        vs = list(poly.vertices)
        n = len(vs)
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
        if uv_layer and uvsp:
            uv_ids = []
            for k in range(n):
                loop_i = poly.loop_start + k
                uv = uv_layer.data[loop_i].uv
                uv_ids.append(uv_lookup[(round(uv.x, 6), round(uv.y, 6))])
            rec += "\t\tmu 0 %d %s\n" % (n, " ".join(str(u) for u in uv_ids))
        frecs.append(rec)
    if np:
        w('\tsetAttr -s %d ".fc[0:%d]" -type "polyFaces"\n%s\t\t;\n'
          % (np, np - 1, "".join(frecs)))

    # vertex colours (first colour attribute), as colorSet0
    if want_colors:
        _emit_vertex_colors(w, mesh)

    # custom normals
    if want_normals:
        _emit_normals(w, mesh, full3)

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

    eval_obj.to_mesh_clear()
    return dict(shape=sname, transform=tname, nverts=nv)


def _emit_vertex_colors(w, mesh):
    col = None
    for attr in mesh.color_attributes:
        col = attr
        break
    if col is None:
        return
    w('\tsetAttr ".ccls" -type "string" "colorSet0";\n')
    w('\tsetAttr ".clst[0].clsn" -type "string" "colorSet0";\n')
    # per-corner colours
    n = len(mesh.loops)
    if col.domain == 'POINT':
        data = [col.data[v].color for v in
                (loop.vertex_index for loop in mesh.loops)]
    else:
        data = [c.color for c in col.data]
    flat = " ".join("%s %s %s %s" % (_f(c[0]), _f(c[1]), _f(c[2]),
                                     _f(c[3] if len(c) > 3 else 1.0))
                    for c in data)
    if data:
        w('\tsetAttr -s %d ".clst[0].clsp[0:%d]" -type "float4" %s;\n'
          % (len(data), len(data) - 1, flat))


def _emit_normals(w, mesh, em3):
    try:
        mesh.calc_normals_split()
    except Exception:
        pass
    n = len(mesh.loops)
    if not n:
        return
    chunks = []
    for loop in mesh.loops:
        nrm = loop.normal
        chunks.append(" ".join(_f(c) for c in nrm))
    w('\tsetAttr -s %d ".n[0:%d]" -type "float3" %s;\n'
      % (n, n - 1, "  ".join(chunks)))


# ---------------------------------------------------------------------------
# skeleton
# ---------------------------------------------------------------------------
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
        world = em @ (arm_world @ bone.matrix_local)
        if parent:
            pworld = em @ (arm_world @ parent.matrix_local)
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
        count += 1
    if not count:
        return 0
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
