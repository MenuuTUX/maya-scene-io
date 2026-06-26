# SPDX-License-Identifier: GPL-3.0-or-later
"""
Turns a parsed :class:`ma_parser.MayaScene` into clean, Blender-ready data
structures (still free of any ``bpy`` import, so the whole extraction pipeline
can be unit tested with a stock Python interpreter).

Nothing here touches the Blender API; the companion ``scene_builder`` module
consumes these dataclasses and creates the actual objects, applying the
coordinate-system conversion and matrix maths with ``mathutils``.
"""

import math
import re
from collections import deque

try:                                    # package (inside Blender)
    from . import ma_parser
    from .ma_parser import as_floats, as_bool
except ImportError:                      # standalone (unit testing)
    import ma_parser
    from ma_parser import as_floats, as_bool


# ---------------------------------------------------------------------------
# data containers
# ---------------------------------------------------------------------------
class MeshData:
    def __init__(self, name):
        self.name = name              # the transform (object) name
        self.shape = None             # geometry shape node name
        self.render_shape = None      # visible shape node name (for lookups)
        self.verts = []               # [(x, y, z), ...]
        self.edges = []               # [(a, b, sharp_bool), ...]
        self.faces = []               # [[vidx, ...], ...]
        self.face_uvs = []            # [[uvidx, ...] | None, ...]  (per face)
        self.uv_sets = []             # [(name, [(u, v), ...]), ...]
        self.face_uv_sets = []        # per uv-set: [[uvidx,...]|None per face]
        self.normals = None           # optional [(x, y, z), ...] per face-corner
        self.world_matrix = None      # 16 floats (Maya row-major) or None
        self.is_skinned = False
        self.hidden = False           # hidden in Maya (.v off on self or a parent)


class JointData:
    def __init__(self, name):
        self.name = name
        self.parent = None            # immediate Maya DAG parent (any type)
        self.bone_parent = None       # nearest joint ancestor (for the armature)
        self.bps = None               # 16 floats world bind matrix or None
        self.t = (0.0, 0.0, 0.0)
        self.r = (0.0, 0.0, 0.0)
        self.jo = (0.0, 0.0, 0.0)
        self.s = (1.0, 1.0, 1.0)
        self.ro = 0
        self.ssc = True
        self.radius = 1.0
        self.custom = {}              # name -> value (custom attributes)


class SkinData:
    def __init__(self, name):
        self.name = name
        self.mesh = None              # transform name of deformed mesh
        self.influences = {}          # influence index -> joint name
        # vertex index -> {joint name: weight}
        self.weights = {}


class ShapeData:
    def __init__(self, name):
        self.name = name
        self.deltas = {}              # vertex index -> (dx, dy, dz)


class MaterialData:
    def __init__(self, name):
        self.name = name
        self.node_type = 'lambert'
        self.color = (0.5, 0.5, 0.5)
        self.transparency = (0.0, 0.0, 0.0)
        self.incandescence = (0.0, 0.0, 0.0)
        self.base_texture = None      # resolved/expected base colour map name
        self.tex_prefix = None        # e.g. "Body" -> Body_BaseColor.png ...
        self.maya_textures = {}       # slot -> original path string


class CurveData:
    def __init__(self, name):
        self.name = name
        self.degree = 1
        self.form = 0                 # 0 open, 1 closed, 2 periodic
        self.cvs = []                 # [(x, y, z), ...]
        self.world_matrix = None
        self.parent_joint = None      # nearest joint ancestor (for following rig)
        self.color = None             # wire colour (r, g, b) if known


class ConstraintData:
    def __init__(self, name, ctype):
        self.name = name
        self.ctype = ctype            # point/orient/parent/scale/aim
        self.constrained = None       # driven node name
        self.constrained_is_joint = False
        self.targets = []             # [(node, weight), ...]


class IKData:
    def __init__(self, name):
        self.name = name
        self.start_joint = None       # chain root
        self.end_joint = None         # chain tip (effector's joint)
        self.solver = None            # ikSCsolver / ikRPsolver / ikSplineSolver
        self.pole = None              # pole-vector target node, if any


# ---------------------------------------------------------------------------
# geometry reconstruction
# ---------------------------------------------------------------------------
def _reconstruct_geometry(mesh):
    """Rebuild verts / edges / faces / per-face uv indices from a mesh node."""
    vt = as_floats(mesh.attr_tokens('vt'))
    verts = [(vt[i], vt[i + 1], vt[i + 2]) for i in range(0, len(vt), 3)]

    et = mesh.attr_tokens('ed')
    edges = []
    for i in range(0, len(et), 3):
        a = int(float(et[i]))
        b = int(float(et[i + 1]))
        sharp = (et[i + 2] == '0')           # flag 0 == hard/sharp edge
        edges.append((a, b, sharp))

    # apply vertex tweaks (.pt[i] = float3 offset added to the base position)
    for name, atype, toks in mesh.set_attrs:
        if name.startswith('pt['):
            m = re.match(r'pt\[(\d+)\]', name)
            if not m:
                continue
            vi = int(m.group(1))
            off = as_floats(toks)
            if vi < len(verts) and len(off) >= 3:
                x, y, z = verts[vi]
                verts[vi] = (x + off[0], y + off[1], z + off[2])

    # polyFaces -> face vertex loops + per uv-set face indices
    fc = mesh.attr_tokens('fc')
    faces = []
    n_uvsets = max(1, len(mesh.attr('uvst')))
    faces_uv = []     # list per face of dict(uvset -> [uvidx,...])
    i, n = 0, len(fc)
    cur = None
    cur_uv = None
    while i < n:
        rec = fc[i]; i += 1
        if rec == 'f':
            if cur is not None:
                faces.append(cur); faces_uv.append(cur_uv)
            c = int(fc[i]); i += 1
            loop = []
            for _ in range(c):
                e = int(fc[i]); i += 1
                if e >= 0:
                    loop.append(edges[e][0])
                else:
                    loop.append(edges[-e - 1][1])
            cur = loop
            cur_uv = {}
        elif rec == 'mu':
            uvset = int(fc[i]); i += 1
            c = int(fc[i]); i += 1
            cur_uv[uvset] = [int(fc[i + k]) for k in range(c)]
            i += c
        elif rec == 'mf':
            c = int(fc[i]); i += 1
            cur_uv[0] = [int(fc[i + k]) for k in range(c)]
            i += c
        elif rec == 'mc':
            i += 1                         # colour set index
            c = int(fc[i]); i += 1
            i += c
        elif rec == 'h':
            c = int(fc[i]); i += 1
            i += c                         # hole edges (ignored)
        else:
            break
    if cur is not None:
        faces.append(cur); faces_uv.append(cur_uv)

    return verts, edges, faces, faces_uv, n_uvsets


def _uv_coords(mesh, uvset_index):
    """Return (set_name, [(u, v), ...]) for a uv set."""
    entries = mesh.attr('uvst')
    name = "map%d" % (uvset_index + 1)
    coords = []
    pref = "uvst[%d]" % uvset_index
    for ename, atype, toks in mesh.set_attrs:
        if ename.startswith(pref + ".uvsn"):
            if toks:
                name = toks[0]
        elif ename.startswith(pref + ".uvsp"):
            fl = as_floats(toks)
            for j in range(0, len(fl), 2):
                coords.append((fl[j], fl[j + 1]))
    return name, coords


# ---------------------------------------------------------------------------
# the importer model
# ---------------------------------------------------------------------------
# node types that simply pass geometry through (used when tracing a deformer
# chain forward to the mesh it ultimately drives)
_PASSTHROUGH = {
    'groupParts', 'groupId', 'transformGeometry', 'tweak', 'polyTweak',
    'polySoftEdge', 'polyExtrudeFace', 'polyCreateFace', 'polyCylinder',
    'deltaMush', 'tension', 'blendShape', 'skinCluster', 'wrap', 'wire',
    'ffd', 'cluster', 'nonLinear', 'sculpt', 'softMod',
}


class MAImport:
    def __init__(self, scene):
        self.scene = scene
        self._mesh_by_shape = None
        self._geom_flow = None

    # -- relationship helpers --------------------------------------------
    def _shape_to_transform(self, shape_name):
        node = self.scene.nodes.get(shape_name)
        return node.parent if node else None

    def _build_geom_flow(self):
        """Adjacency of geometry data flow (ignoring message links)."""
        adj = {}
        for sn, sa, dn, da in self.scene.connections:
            if sa in ('msg', 'message') or da in ('msg', 'message'):
                continue
            adj.setdefault(sn, []).append(dn)
        self._geom_flow = adj

    def _nearest_mesh(self, start_node):
        """BFS forward from a deformer to the first mesh shape it reaches."""
        if self._geom_flow is None:
            self._build_geom_flow()
        seen = {start_node}
        q = deque(self._geom_flow.get(start_node, []))
        while q:
            cur = q.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            node = self.scene.nodes.get(cur)
            if node is None:
                continue
            if node.ntype == 'mesh':
                return cur
            if node.ntype in _PASSTHROUGH:
                q.extend(self._geom_flow.get(cur, []))
        return None

    # -- meshes ----------------------------------------------------------
    def meshes(self):
        """One MeshData per transform that owns renderable geometry."""
        out = []
        by_transform = {}
        for m in self.scene.of_type('mesh'):
            tr = m.parent
            by_transform.setdefault(tr, []).append(m)

        for tr, shapes in by_transform.items():
            # geometry shape = the one carrying .vt + .fc
            geo = None
            render = None
            for sh in shapes:
                io = sh.attr_tokens('io')
                is_io = bool(io and io[0] in ('yes', '1', 'on', 'true'))
                if not is_io:
                    render = sh.name
                if sh.has_attr('vt') and sh.has_attr('fc'):
                    if geo is None or not is_io:
                        # prefer a non-intermediate geo shape if one exists,
                        # else fall back to the Orig
                        if geo is None:
                            geo = sh.name
                        else:
                            geo_node = self.scene.nodes[geo]
                            gio = geo_node.attr_tokens('io')
                            if gio and gio[0] in ('yes', '1', 'on', 'true'):
                                geo = sh.name
            if geo is None:
                continue
            if render is None:
                render = geo

            md = MeshData(tr or geo)
            md.shape = geo
            md.render_shape = render
            gnode = self.scene.nodes[geo]
            (md.verts, md.edges, md.faces,
             faces_uv, n_uvsets) = _reconstruct_geometry(gnode)

            for s in range(n_uvsets):
                set_name, coords = _uv_coords(gnode, s)
                if not coords:
                    continue
                per_face = [fu.get(s) if fu else None for fu in faces_uv]
                md.uv_sets.append((set_name, coords))
                md.face_uv_sets.append(per_face)

            # is this geometry driven by a skinCluster?
            md.is_skinned = self._mesh_is_skinned(render)
            # respect Maya visibility: hidden if .v is off on the transform or
            # any ancestor (catches the FaceFitSkeleton rig-fit helpers and the
            # closed-mouth teeth/tongue Maya keeps hidden)
            md.hidden = self._hidden_by_visibility(tr) or \
                self._hidden_by_visibility(geo)
            out.append(md)
        return out

    def _hidden_by_visibility(self, node_name):
        cur = self.scene.nodes.get(node_name)
        depth = 0
        while cur is not None and depth < 256:
            v = cur.attr_tokens('v')
            if v and v[0] in ('no', '0', 'off', 'false'):
                return True
            cur = self.scene.nodes.get(cur.parent) if cur.parent else None
            depth += 1
        return False

    def _mesh_is_skinned(self, render_shape):
        for sc in self.scene.of_type('skinCluster'):
            if self._nearest_mesh(sc.name) == render_shape:
                return True
        return False

    # -- skeleton --------------------------------------------------------
    def joints_in_order(self):
        """All joints, bone-parents guaranteed before children."""
        joints = {j.name: j for j in self.scene.of_type('joint')}
        self._joint_name_set = set(joints)
        ordered = []
        emitted = set()

        def emit(name):
            if name in emitted or name not in joints:
                return
            bp = self._nearest_joint_ancestor(name)
            if bp in joints and bp not in emitted:
                emit(bp)
            emitted.add(name)
            ordered.append(self._joint_data(joints[name]))

        for n in self.scene.order:
            if n in joints:
                emit(n)
        return ordered

    def _nearest_joint_ancestor(self, name):
        """Walk up the Maya DAG (through non-joint group transforms) to the
        first joint ancestor, so bones reconnect across Maya's group nodes."""
        names = getattr(self, '_joint_name_set', None)
        if names is None:
            names = self._joint_name_set = set(self.scene.by_type.get('joint', []))
        cur = self.scene.nodes.get(name)
        cur = self.scene.nodes.get(cur.parent) if cur and cur.parent else None
        depth = 0
        while cur is not None and depth < 256:
            if cur.name in names:
                return cur.name
            cur = self.scene.nodes.get(cur.parent) if cur.parent else None
            depth += 1
        return None

    def _joint_data(self, node):
        jd = JointData(node.name)
        jd.parent = node.parent
        jd.bone_parent = self._nearest_joint_ancestor(node.name)
        if node.has_attr('bps'):
            fl = as_floats(node.attr_tokens('bps'))
            if len(fl) >= 16:
                jd.bps = fl[:16]
        jd.t = tuple(as_floats(node.attr_tokens('t'))[:3]) or (0, 0, 0)
        jd.r = tuple(as_floats(node.attr_tokens('r'))[:3]) or (0, 0, 0)
        jd.jo = tuple(as_floats(node.attr_tokens('jo'))[:3]) or (0, 0, 0)
        s = as_floats(node.attr_tokens('s'))
        jd.s = tuple(s[:3]) if len(s) >= 3 else (1.0, 1.0, 1.0)
        ro = node.attr_tokens('ro')
        jd.ro = int(float(ro[0])) if ro else 0
        ssc = node.attr_tokens('ssc')
        jd.ssc = not (ssc and ssc[0] in ('no', '0', 'off', 'false'))
        radi = node.attr_tokens('radi')
        if radi:
            try:
                jd.radius = float(radi[0])
            except ValueError:
                pass
        jd.t = _pad3(jd.t); jd.r = _pad3(jd.r); jd.jo = _pad3(jd.jo)
        # custom attributes (preserve the rig's per-joint metadata)
        for spec in node.add_attrs:
            key = spec.get('ln') or spec.get('sn')
            if not key:
                continue
            val = node.attr_tokens(key) or node.attr_tokens(spec.get('sn', ''))
            if val:
                try:
                    jd.custom[key] = float(val[0])
                except ValueError:
                    jd.custom[key] = val[0]
            elif 'dv' in spec:
                try:
                    jd.custom[key] = float(spec['dv'])
                except ValueError:
                    pass
        return jd

    # -- skinning --------------------------------------------------------
    def skins(self):
        out = []
        for sc in self.scene.of_type('skinCluster'):
            mesh_shape = self._nearest_mesh(sc.name)
            if mesh_shape is None:
                continue
            sd = SkinData(sc.name)
            sd.mesh = self._shape_to_transform(mesh_shape) or mesh_shape
            # influence index -> joint
            for attr, srcs in self.scene.iter_dest_prefix(sc.name, 'ma['):
                m = re.search(r'ma\[(\d+)\]', attr)
                if m and srcs:
                    sd.influences[int(m.group(1))] = srcs[0][0]
            # weights
            self._decode_weights(sc, sd)
            out.append(sd)
        return out

    def _decode_weights(self, sc, sd):
        for name, atype, toks in sc.set_attrs:
            if not (name.startswith('wl[') and name.endswith('.w')):
                continue
            m = re.search(r'wl\[(\d+)(?::(\d+))?\]', name)
            if not m:
                continue
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            i = 0
            ln = len(toks)
            for vtx in range(a, b + 1):
                if i >= ln:
                    break
                cnt = int(toks[i]); i += 1
                wmap = {}
                for _ in range(cnt):
                    inf = int(toks[i]); w = float(toks[i + 1]); i += 2
                    joint = sd.influences.get(inf)
                    if joint and w != 0.0:
                        wmap[joint] = wmap.get(joint, 0.0) + w
                if wmap:
                    sd.weights[vtx] = wmap

    # -- blend shapes ----------------------------------------------------
    def blendshapes(self):
        """{mesh transform: [ShapeData, ...]} for mesh-deforming blendShapes."""
        out = {}
        for bs in self.scene.of_type('blendShape'):
            targets = self._blendshape_targets(bs)
            if not targets:
                continue
            mesh_shape = self._nearest_mesh(bs.name)
            if mesh_shape is None:
                continue
            tr = self._shape_to_transform(mesh_shape) or mesh_shape
            out.setdefault(tr, []).extend(targets)
        return out

    def _blendshape_targets(self, bs):
        aliases = self._target_aliases(bs)
        # gather geometry targets grouped by itg index
        groups = {}
        for name, atype, toks in bs.set_attrs:
            mi = re.match(r'it\[0\]\.itg\[(\d+)\]\.iti\[(\d+)\]\.(ipt|ict)', name)
            if not mi:
                continue
            g = int(mi.group(1))
            which = mi.group(3)
            groups.setdefault(g, {})[which] = toks
        targets = []
        for g in sorted(groups):
            data = groups[g]
            if 'ipt' not in data:
                continue
            pts = _decode_point_array(data['ipt'])
            comps = _decode_component_list(data.get('ict', []))
            sd = ShapeData(aliases.get(g, "target_%d" % g))
            if comps and len(comps) == len(pts):
                for vi, p in zip(comps, pts):
                    sd.deltas[vi] = p
            else:
                # no component list -> dense, indexed from 0
                for vi, p in enumerate(pts):
                    sd.deltas[vi] = p
            if sd.deltas:
                targets.append(sd)
        return targets

    def _target_aliases(self, bs):
        """Map weight index -> alias name from the .aal attributeAlias."""
        out = {}
        for name, atype, toks in bs.set_attrs:
            if name != 'aal':
                continue
            # tokens look like: { "name", "weight[0]", "name2", "weight[1]" }
            cleaned = [t for t in toks if t not in ('{', '}', ',')]
            for k in range(0, len(cleaned) - 1, 2):
                alias = cleaned[k]
                wm = re.search(r'weight\[(\d+)\]', cleaned[k + 1])
                if wm:
                    out[int(wm.group(1))] = alias
        return out

    # -- materials -------------------------------------------------------
    def materials(self):
        """{shadingEngine name: MaterialData}."""
        out = {}
        for sg in self.scene.of_type('shadingEngine'):
            srcs = self.scene.sources_of(sg.name, 'ss')
            if not srcs:
                continue
            mat_node = self.scene.nodes.get(srcs[0][0])
            if mat_node is None:
                continue
            out[sg.name] = self._material_data(mat_node)
        return out

    # base-colour attribute across material types: lambert/blinn/phong use .c,
    # Arnold aiStandardSurface and Autodesk standardSurface use .baseColor/.bc,
    # some shaders use .color / .dc. Try them in order.
    _COLOR_ATTRS = ('c', 'baseColor', 'bc', 'color', 'dc')
    _EMISSION_ATTRS = ('it', 'emissionColor', 'ec')

    def _material_data(self, node):
        md = MaterialData(node.name)
        md.node_type = node.ntype
        for a in self._COLOR_ATTRS:
            c = as_floats(node.attr_tokens(a))
            if len(c) >= 3:
                md.color = tuple(c[:3])
                break
        for a in self._EMISSION_ATTRS:
            e = as_floats(node.attr_tokens(a))
            if len(e) >= 3:
                md.incandescence = tuple(e[:3])
                break
        trn = as_floats(node.attr_tokens('tc'))
        if len(trn) >= 3:
            md.transparency = tuple(trn[:3])
        # texture wired into the base colour, whichever attribute holds it
        for a in self._COLOR_ATTRS:
            for src, sattr in self.scene.sources_of(node.name, a):
                snode = self.scene.nodes.get(src)
                if snode and snode.ntype == 'file':
                    ftn = snode.attr_tokens('ftn')
                    if ftn:
                        md.maya_textures['baseColor'] = ftn[0]
                        md.tex_prefix = _texture_prefix(ftn[0])
            if 'baseColor' in md.maya_textures:
                break
        if md.tex_prefix is None:
            md.tex_prefix = _texture_prefix_from_name(node.name)
        return md

    def mesh_material_map(self):
        """{shape name: [(shadingEngine, face_indices | None), ...]}.

        Ordered by Maya object-group index so material slots line up.  A
        ``None`` face list means the shading engine covers the whole object
        (the common single-material case); an explicit list carries the
        per-face assignment (e.g. the helmet's glass visor)."""
        tmp = {}
        for sg in self.scene.of_type('shadingEngine'):
            for attr, srcs in self.scene.iter_dest_prefix(sg.name, 'dsm'):
                for sn, sa in srcs:
                    m = re.search(r'og\[(\d+)\]', sa)
                    og = int(m.group(1)) if m else 0
                    faces = self._face_group(sn, og)
                    tmp.setdefault(sn, []).append((og, sg.name, faces))
        out = {}
        for sn, items in tmp.items():
            items.sort(key=lambda x: x[0])
            out[sn] = [(sg, faces) for _, sg, faces in items]
        return out

    def _face_group(self, shape_name, og_index):
        node = self.scene.nodes.get(shape_name)
        if node is None:
            return None
        # 1) inline component list on the shape (static meshes)
        needle = "og[%d].gcl" % og_index
        for name, atype, toks in node.set_attrs:
            if name.endswith(needle) and 'iog' in name:
                return _decode_face_list(toks)
        # 2) deformed meshes: og[k] -> groupId -> groupParts.ic component list
        gid_srcs = self.scene.sources_of(shape_name, "iog.og[%d].gid" % og_index)
        for gid_node, _gid_attr in gid_srcs:
            for dn, da in self.scene.dests_of(gid_node, 'id'):
                dnode = self.scene.nodes.get(dn)
                if dnode is not None and dnode.ntype == 'groupParts':
                    faces = _decode_face_list(dnode.attr_tokens('ic'))
                    if faces:
                        return faces
        return None

    # -- nurbs control curves -------------------------------------------
    def curves(self):
        out = []
        for cv in self.scene.of_type('nurbsCurve'):
            io = cv.attr_tokens('io')
            if io and io[0] in ('yes', '1', 'on', 'true'):
                continue
            data = cv.attr_tokens('cc')
            pts, deg, form = _decode_nurbs_curve(data)
            if not pts:
                continue
            cd = CurveData(cv.parent or cv.name)
            cd.cvs = pts
            cd.degree = deg
            cd.form = form
            cd.parent_joint = self._nearest_joint_ancestor(cv.parent or cv.name)
            # wire colour from a drawing override, when authored
            oc = as_floats((cv.parent and self.scene.nodes.get(cv.parent) or cv)
                           .attr_tokens('ovrgb'))
            if len(oc) >= 3:
                cd.color = tuple(oc[:3])
            out.append(cd)
        return out

    # -- constraints (experimental rig) ---------------------------------
    _CONSTRAINT_OUT = ('ctx', 'cty', 'ctz', 'crx', 'cry', 'crz',
                       'csx', 'csy', 'csz')
    _CONSTRAINT_TYPES = ('pointConstraint', 'orientConstraint',
                         'parentConstraint', 'scaleConstraint', 'aimConstraint')

    def constraints(self):
        names = getattr(self, '_joint_name_set', None) or \
            set(self.scene.by_type.get('joint', []))
        self._joint_name_set = names
        out = []
        for ctype in self._CONSTRAINT_TYPES:
            short = ctype.replace('Constraint', '')
            for node in self.scene.of_type(ctype):
                cd = ConstraintData(node.name, short)
                # constrained object = where any output attr connects to
                for oa in self._CONSTRAINT_OUT:
                    dests = self.scene.dests_of(node.name, oa)
                    if dests:
                        cd.constrained = dests[0][0]
                        break
                if cd.constrained is None:
                    continue
                cd.constrained_is_joint = cd.constrained in names
                # targets = sources feeding tg[i].tt / .tr / .ts
                tgt = {}
                for sub in ('tt', 'tr', 'ts', 'tpm'):
                    for attr, srcs in self.scene.iter_dest_prefix(node.name, 'tg['):
                        if not attr.endswith('.' + sub):
                            continue
                        m = re.search(r'tg\[(\d+)\]', attr)
                        if m and srcs:
                            tgt.setdefault(int(m.group(1)), srcs[0][0])
                cd.targets = [(tgt[i], 1.0) for i in sorted(tgt)]
                if cd.targets:
                    out.append(cd)
        return out

    def ik_handles(self):
        names = getattr(self, '_joint_name_set', None) or \
            set(self.scene.by_type.get('joint', []))
        out = []
        for ik in self.scene.of_type('ikHandle'):
            d = IKData(ik.parent or ik.name)
            for sn, sa in self.scene.sources_of(ik.name, 'hsj'):
                d.start_joint = sn
            eff = None
            for sn, sa in self.scene.sources_of(ik.name, 'hee'):
                eff = sn
            if eff is not None:
                effnode = self.scene.nodes.get(eff)
                d.end_joint = effnode.parent if effnode else None
            for sn, sa in self.scene.sources_of(ik.name, 'hsv'):
                d.solver = sn
            if d.start_joint in names and d.end_joint in names:
                out.append(d)
        return out

    # -- cameras --------------------------------------------------------
    def cameras(self):
        """Non-default cameras as dicts(name, transform, focal, world)."""
        defaults = {'persp', 'top', 'front', 'side', 'left', 'bottom', 'back'}
        out = []
        for cam in self.scene.of_type('camera'):
            tr = cam.parent or cam.name
            if tr in defaults:
                continue
            fl = as_floats(cam.attr_tokens('fl'))
            out.append(dict(name=tr, shape=cam.name,
                            focal=fl[0] if fl else 35.0,
                            ortho=as_bool(cam.attr_tokens('o'))))
        return out

    # -- lights ---------------------------------------------------------
    _LIGHT_TYPES = {
        'directionalLight': 'SUN', 'pointLight': 'POINT',
        'spotLight': 'SPOT', 'ambientLight': 'POINT', 'areaLight': 'AREA',
        'volumeLight': 'POINT',
    }

    def lights(self):
        out = []
        for ltype, btype in self._LIGHT_TYPES.items():
            for ln in self.scene.of_type(ltype):
                col = as_floats(ln.attr_tokens('cl'))
                inten = as_floats(ln.attr_tokens('in'))
                cone = as_floats(ln.attr_tokens('ca'))   # spot cone angle
                out.append(dict(name=ln.parent or ln.name, shape=ln.name,
                                btype=btype,
                                color=tuple(col[:3]) if len(col) >= 3 else (1, 1, 1),
                                energy=inten[0] if inten else 1.0,
                                spot_angle=cone[0] if cone else 40.0))
        return out

    # -- animation (time-based animCurves) ------------------------------
    _ANIM_ATTR = {
        'tx': ('location', 0), 'ty': ('location', 1), 'tz': ('location', 2),
        'rx': ('rotation_euler', 0), 'ry': ('rotation_euler', 1),
        'rz': ('rotation_euler', 2),
        'sx': ('scale', 0), 'sy': ('scale', 1), 'sz': ('scale', 2),
    }

    def animations(self):
        """{node name: [ (blender_path, array_index, [(frame, value), ...]) ]}
        for time-based animCurves only (TA/TL/TU/TT)."""
        out = {}
        for ctype in ('animCurveTL', 'animCurveTA', 'animCurveTU', 'animCurveTT'):
            for cv in self.scene.of_type(ctype):
                keys = self._anim_keys(cv, ctype)
                if not keys:
                    continue
                for dn, da in self.scene.dests_of(cv.name, 'o'):
                    mapping = self._ANIM_ATTR.get(da)
                    if not mapping:
                        continue
                    path, idx = mapping
                    is_rot = path == 'rotation_euler'
                    pts = [(t, math.radians(v) if is_rot else v) for t, v in keys]
                    out.setdefault(dn, []).append((path, idx, pts))
        return out

    def _anim_keys(self, cv, ctype):
        toks = cv.attr_tokens('ktv')
        fl = as_floats(toks)
        keys = []
        for i in range(0, len(fl) - 1, 2):
            keys.append((fl[i], fl[i + 1]))
        return keys


# ---------------------------------------------------------------------------
# small decoders / helpers
# ---------------------------------------------------------------------------
def _pad3(t):
    t = list(t)
    while len(t) < 3:
        t.append(0.0)
    return tuple(t[:3])


def _decode_point_array(toks):
    """Maya 'pointArray': <count> then count*(x y z w)."""
    if not toks:
        return []
    try:
        count = int(toks[0])
    except (ValueError, IndexError):
        return []
    fl = as_floats(toks[1:])
    pts = []
    # 4 components per point (x, y, z, w); fall back to 3 if that fits better
    stride = 4 if len(fl) >= count * 4 else 3
    for i in range(count):
        base = i * stride
        if base + 2 < len(fl):
            pts.append((fl[base], fl[base + 1], fl[base + 2]))
    return pts


def _decode_component_list(toks):
    """Maya 'componentList': <count> then strings like vtx[3], vtx[5:9]."""
    out = []
    for t in toks:
        m = re.match(r'(?:vtx|cp|pt)\[(\d+)(?::(\d+))?\]', t)
        if not m:
            continue
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        out.extend(range(a, b + 1))
    return out


def _decode_face_list(toks):
    """Maya face 'componentList': <count> then f[3], f[5:9], ..."""
    out = []
    for t in toks:
        m = re.match(r'f\[(\d+)(?::(\d+))?\]', t)
        if not m:
            continue
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        out.extend(range(a, b + 1))
    return out


def _decode_nurbs_curve(toks):
    """Decode a cached nurbsCurve (.cc) header + CV list. Best effort."""
    if not toks:
        return [], 1, 0
    # header: degree spans form rational dimension  then knots, then CVs
    try:
        degree = int(toks[0])
        spans = int(toks[1])
        form = int(toks[2])
        is_rational = toks[3] in ('yes', '1', 'true')
        dim = int(toks[4])
    except (ValueError, IndexError):
        return [], 1, 0
    idx = 5
    n_knots_decl = None
    # Some files write the knot count explicitly next; detect numerically.
    # Maya layout: <knotCount> <knots...> <cvCount> <cv x y z ...>
    try:
        n_knots = int(toks[idx]); idx += 1
    except (ValueError, IndexError):
        return [], degree, form
    idx += n_knots                       # skip knots
    try:
        n_cvs = int(toks[idx]); idx += 1
    except (ValueError, IndexError):
        return [], degree, form
    fl = as_floats(toks[idx:])
    stride = dim + (1 if is_rational else 0)
    pts = []
    for i in range(n_cvs):
        base = i * stride
        if base + 2 < len(fl):
            pts.append((fl[base], fl[base + 1], fl[base + 2] if dim >= 3 else 0.0))
    return pts, degree, form


def _texture_prefix(path):
    base = re.split(r'[\\/]', path)[-1]
    base = re.sub(r'\.[A-Za-z0-9]+$', '', base)
    for suffix in ('_BaseColor', '_Albedo', '_Diffuse', '_Color', '_Normal',
                   '_Roughness', '_Metallic', '_Emissive', '_BaseColour'):
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base


def _texture_prefix_from_name(name):
    n = re.sub(r'(_MAT|_mat|Mat|_Material|SG)$', '', name)
    return n


def extract(scene):
    return MAImport(scene)
