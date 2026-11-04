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
    from .ma_parser import as_floats, as_bool, value_tokens, LazyValues
except ImportError:                      # standalone (unit testing)
    import ma_parser
    from ma_parser import as_floats, as_bool, value_tokens, LazyValues


# ---------------------------------------------------------------------------
# data containers
# ---------------------------------------------------------------------------
_OFF = ('no', '0', 'off', 'false')


def _is_intermediate(node):
    io = node.attr_tokens('io')
    return bool(io and io[0] in ('yes', '1', 'on', 'true'))


class MeshData:
    def __init__(self, name):
        self.name = name              # the transform (object) name
        self.shape = None             # geometry shape node name
        self.render_shape = None      # visible shape node name (for lookups)
        self.verts = []               # [(x, y, z), ...]
        self.edges = []               # [(a, b, sharp_bool), ...]
        self.faces = []               # [[vidx, ...], ...]
        self.uv_sets = []             # [(name, [(u, v), ...]), ...]
        self.face_uv_sets = []        # per uv-set: [[uvidx,...]|None per face]
        self.color_sets = []          # [(name, [(r, g, b, a), ...], per-face ids)]
        self.active_color = 0         # index into color_sets
        self.is_skinned = False
        self.skin_geom = None         # skinCluster geomMatrix (16 floats) or None
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
        self.weight = 0.0             # authored blendShape weight


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
        self.normal_mode = 1          # bump2d: 0 bump, 1 tangent, 2 object space
        self.bump_depth = 1.0
        self.metalness = None         # None: shader has no such value
        self.roughness = None
        self.emission_strength = 1.0  # Maya's incandescence has no weight


class CurveData:
    def __init__(self, name):
        self.name = name
        self.degree = 1
        self.form = 0                 # 0 open, 1 closed, 2 periodic
        self.cvs = []                 # [(x, y, z), ...]
        self.world_matrix = None
        self.parent_joint = None      # nearest joint ancestor (for following rig)
        self.color = None             # wire colour (r, g, b) if known


class IKData:
    def __init__(self, name):
        self.name = name
        self.start_joint = None       # chain root
        self.end_joint = None         # joint the effector sits on (wrist)
        self.eff_parent = None        # joint the effector hangs under (elbow)
        self.solver = None            # ikSCsolver / ikRPsolver / ikSplineSolver


# ---------------------------------------------------------------------------
# geometry reconstruction
# ---------------------------------------------------------------------------
def _reconstruct_geometry(mesh):
    """Rebuild verts / edges / faces / per-face uv indices from a mesh node."""
    problems = []
    vt = as_floats(mesh.attr_tokens('vt'))
    if not all(map(math.isfinite, vt)):      # nan / inf would poison the mesh
        vt = [x if math.isfinite(x) else 0.0 for x in vt]
        problems.append("non-finite vertex positions were set to 0")
    verts = [(vt[i], vt[i + 1], vt[i + 2]) for i in range(0, len(vt) - 2, 3)]

    et = mesh.attr_tokens('ed')
    edges = []
    try:
        for i in range(0, len(et) - 2, 3):
            a = int(float(et[i]))
            b = int(float(et[i + 1]))
            sharp = (et[i + 2] == '0')           # flag 0 == hard/sharp edge
            edges.append((a, b, sharp))
    except (ValueError, OverflowError):
        problems.append("edge data is damaged; %d edges kept" % len(edges))

    # apply vertex tweaks (.pt[i] or .pt[a:b] = float3 offsets added to the
    # base positions)
    for name, atype, toks in mesh.set_attrs:
        if name.startswith('pt['):
            m = re.match(r'pt\[(\d+)(?::(\d+))?\]$', name)
            if not m:
                continue
            first = int(m.group(1))
            last = int(m.group(2)) if m.group(2) else first
            off = as_floats(value_tokens(toks))
            for k, vi in enumerate(range(first, last + 1)):
                if vi >= len(verts) or len(off) < 3 * k + 3:
                    break
                x, y, z = verts[vi]
                verts[vi] = (x + off[3 * k], y + off[3 * k + 1],
                             z + off[3 * k + 2])

    # polyFaces -> face vertex loops + per uv-set face indices
    fc = mesh.attr_tokens('fc')
    faces = []
    n_uvsets = max(1, len(mesh.attr('uvst')))
    faces_uv = []     # list per face of dict(uvset -> [uvidx,...])
    faces_col = []    # list per face of dict(colour set -> [colour id,...])
    try:
        unknown = _read_faces(fc, edges, faces, faces_uv, faces_col)
        if unknown:
            problems.append("unknown face record '%s'; %d faces kept"
                            % (unknown, len(faces)))
    except (IndexError, ValueError, TypeError, OverflowError):
        problems.append("face data is damaged; %d faces kept" % len(faces))

    return verts, edges, faces, faces_uv, n_uvsets, faces_col, problems


def _read_faces(fc, edges, faces, faces_uv, faces_col):
    """Decode a polyFaces token list into ``faces`` / ``faces_uv`` /
    ``faces_col`` in place, so that everything read before a damaged record is
    still there to keep.  Returns the first record type it did not know."""
    i, n = 0, len(fc)
    cur = cur_uv = cur_col = None
    while i < n:
        rec = fc[i]; i += 1
        if rec == 'f':
            if cur is not None:
                faces.append(cur); faces_uv.append(cur_uv)
                faces_col.append(cur_col)
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
            cur_col = {}
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
            cset = int(fc[i]); i += 1             # colour set index
            c = int(fc[i]); i += 1
            cur_col[cset] = [int(fc[i + k]) for k in range(c)]
            i += c
        elif rec == 'h':
            c = int(fc[i]); i += 1
            i += c                         # hole edges (ignored)
        else:
            unknown = rec
            break
    else:
        unknown = None
    if cur is not None:
        faces.append(cur); faces_uv.append(cur_uv); faces_col.append(cur_col)
    return unknown


def _color_sets(mesh):
    """[(name, [(r, g, b, a), ...])] for every colour set, in set order."""
    sets = {}
    for ename, atype, toks in mesh.set_attrs:
        m = re.match(r'clst\[(\d+)\]\.(clsn|clsp)', ename)
        if not m:
            continue
        entry = sets.setdefault(int(m.group(1)), ["colorSet%s" % m.group(1), []])
        values = value_tokens(toks)
        if m.group(2) == 'clsn':
            if values:
                entry[0] = values[0]
        else:
            fl = as_floats(values)
            entry[1].extend(tuple(fl[j:j + 4]) for j in range(0, len(fl) - 3, 4))
    return [(name, colors) for _, (name, colors) in sorted(sets.items())]


def _uv_coords(mesh, uvset_index):
    """Return (set_name, [(u, v), ...]) for a uv set."""
    name = "map%d" % (uvset_index + 1)
    coords = []
    pref = "uvst[%d]" % uvset_index
    for ename, atype, toks in mesh.set_attrs:
        if ename.startswith(pref + ".uvsn"):
            toks = value_tokens(toks)
            if toks:
                name = toks[0]
        elif ename.startswith(pref + ".uvsp"):
            fl = as_floats(value_tokens(toks))
            for j in range(0, len(fl) - 1, 2):
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
        self._geom_flow = None
        self._skin_of_shape = None
        self._hidden_by_layer = None
        self.warnings = []             # damaged items skipped along the way

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
        history_only = []
        by_transform = {}
        for m in self.scene.of_type('mesh'):
            tr = m.parent
            by_transform.setdefault(tr, []).append(m)

        for tr, shapes in by_transform.items():
            try:
                md = self._mesh_data(tr, shapes)
            except Exception as exc:          # one bad mesh must not stop the rest
                self.warnings.append("mesh '%s' skipped: %s" % (tr, exc))
                continue
            if md is not None:
                out.append(md)
            elif not all(_is_intermediate(sh) for sh in shapes):
                history_only.append(tr)
        if history_only:
            self.warnings.append(
                "%d mesh(es) skipped because the file stores only the "
                "construction history that builds them, no geometry (%s%s). "
                "Delete history in Maya, or use the Maya Bridge"
                % (len(history_only), ", ".join(history_only[:5]),
                   ", ..." if len(history_only) > 5 else ""))
        return out

    def _mesh_data(self, tr, shapes):
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
            return None
        if render is None:
            render = geo

        md = MeshData(tr or geo)
        md.shape = geo
        md.render_shape = render
        gnode = self.scene.nodes[geo]
        (md.verts, md.edges, md.faces,
         faces_uv, n_uvsets, faces_col, problems) = _reconstruct_geometry(gnode)
        self.warnings.extend("mesh '%s': %s" % (md.name, p) for p in problems)

        for s in range(n_uvsets):
            set_name, coords = _uv_coords(gnode, s)
            if not coords:
                continue
            per_face = [fu.get(s) if fu else None for fu in faces_uv]
            md.uv_sets.append((set_name, coords))
            md.face_uv_sets.append(per_face)

        for si, (set_name, colors) in enumerate(_color_sets(gnode)):
            if colors:
                md.color_sets.append(
                    (set_name, colors, [fc_.get(si) for fc_ in faces_col]))
        current = gnode.attr_tokens('ccls')
        names = [c[0] for c in md.color_sets]
        md.active_color = names.index(current[0]) \
            if current and current[0] in names else 0

        # is this geometry driven by a skinCluster?
        skin = self._mesh_skin(render)
        md.is_skinned = skin is not None
        if skin is not None:
            gmx = as_floats(skin.attr_tokens('gm'))
            md.skin_geom = gmx[:16] if len(gmx) >= 16 else None
        # respect Maya visibility: hidden if .v is off on the transform or
        # any ancestor (catches the FaceFitSkeleton rig-fit helpers and the
        # closed-mouth teeth/tongue Maya keeps hidden)
        md.hidden = self._hidden_by_visibility(tr) or \
            self._hidden_by_visibility(geo)
        return md

    def _layer_hidden(self):
        """Names of nodes sitting in a display layer that is switched off."""
        if self._hidden_by_layer is None:
            hidden = set()
            for layer in self.scene.of_type('displayLayer'):
                v = layer.attr_tokens('v')
                if v and v[0] in _OFF:
                    hidden.update(dn for dn, _ in
                                  self.scene.dests_of(layer.name, 'di'))
            self._hidden_by_layer = hidden
        return self._hidden_by_layer

    def _hidden_by_visibility(self, node_name):
        cur = self.scene.nodes.get(node_name)
        depth = 0
        while cur is not None and depth < 256:
            v = cur.attr_tokens('v')
            if (v and v[0] in _OFF) or cur.name in self._layer_hidden():
                return True
            cur = self.scene.nodes.get(cur.parent) if cur.parent else None
            depth += 1
        return False

    def _mesh_skin(self, render_shape):
        """The skinCluster deforming a mesh shape, or None."""
        if self._skin_of_shape is None:
            self._skin_of_shape = {}
            for sc in self.scene.of_type('skinCluster'):
                shape = self._nearest_mesh(sc.name)
                if shape is not None:
                    self._skin_of_shape.setdefault(shape, sc)
        return self._skin_of_shape.get(render_shape)

    # -- skeleton --------------------------------------------------------
    def joints_in_order(self):
        """All joints, bone-parents guaranteed before children.  Iterative, so
        a chain thousands of joints deep (a tail, a rope) cannot exhaust the
        recursion limit and a parent loop in a damaged file cannot spin."""
        joints = {j.name: j for j in self.scene.of_type('joint')}
        self._joint_name_set = set(joints)
        ordered = []
        emitted = set()
        for n in self.scene.order:
            if n not in joints or n in emitted:
                continue
            chain, seen = [], set()
            cur = n
            while cur in joints and cur not in emitted and cur not in seen:
                chain.append(cur)
                seen.add(cur)
                cur = self._nearest_joint_ancestor(cur)
            for name in reversed(chain):
                emitted.add(name)
                try:
                    ordered.append(self._joint_data(joints[name]))
                except (ValueError, IndexError, OverflowError) as exc:
                    self.warnings.append("joint '%s' has damaged values (%s); "
                                         "defaults used" % (name, exc))
                    jd = JointData(name)
                    jd.parent = joints[name].parent
                    jd.bone_parent = self._nearest_joint_ancestor(name)
                    ordered.append(jd)
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
        lin, ang = self.scene.cm_per_unit, self.scene.deg_per_unit
        jd.t = tuple(v * lin for v in _pad3(jd.t))
        jd.r = tuple(v * ang for v in _pad3(jd.r))
        jd.jo = tuple(v * ang for v in _pad3(jd.jo))
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
            try:
                sd = self._skin_data(sc)
            except Exception as exc:
                self.warnings.append("skin '%s' skipped: %s" % (sc.name, exc))
                continue
            if sd is not None:
                out.append(sd)
        return out

    def _skin_data(self, sc):
        mesh_shape = self._nearest_mesh(sc.name)
        if mesh_shape is None:
            return None
        sd = SkinData(sc.name)
        sd.mesh = self._shape_to_transform(mesh_shape) or mesh_shape
        # influence index -> joint
        for attr, srcs in self.scene.iter_dest_prefix(sc.name, 'ma['):
            m = re.search(r'ma\[(\d+)\]', attr)
            if m and srcs:
                sd.influences[int(m.group(1))] = srcs[0][0]
        self._decode_weights(sc, sd)
        return sd

    def _decode_weights(self, sc, sd):
        for name, atype, toks in sc.set_attrs:
            if not (name.startswith('wl[') and name.endswith('.w')):
                continue
            m = re.search(r'wl\[(\d+)(?::(\d+))?\]', name)
            if not m:
                continue
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else a
            toks = value_tokens(toks)
            try:
                self._read_weight_block(sd, toks, a, b)
            except (IndexError, ValueError, OverflowError):
                self.warnings.append("skin '%s': weight data is damaged near "
                                     "vertex %s" % (sc.name, a))

    def _read_weight_block(self, sd, toks, a, b):
        i = 0
        ln = len(toks)
        for vtx in range(a, b + 1):
            if i >= ln:
                break
            cnt = int(toks[i]); i += 1
            # Maya may split one vertex's sparse list across two setAttr
            # blocks (wl[0:60] ends and wl[60:117] starts with vertex
            # 60), so merge into what an earlier block already gave.
            wmap = sd.weights.get(vtx, {})
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
            try:
                targets = self._blendshape_targets(bs)
            except Exception as exc:
                self.warnings.append("blendShape '%s' skipped: %s"
                                     % (bs.name, exc))
                continue
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
        weights = {}
        for name, atype, toks in bs.set_attrs:
            mw = re.match(r'w\[(\d+)\]$', name)
            toks = value_tokens(toks) if mw else None
            if mw and toks:
                try:
                    weights[int(mw.group(1))] = float(toks[0])
                except ValueError:
                    pass
        # gather geometry targets grouped by itg index; an in-between shape
        # (iti 5500 and so on) is skipped in favour of the full one (6000)
        groups = {}
        for name, atype, toks in bs.set_attrs:
            mi = re.match(r'it\[0\]\.itg\[(\d+)\]\.iti\[(\d+)\]\.(ipt|ict)', name)
            if not mi:
                continue
            g, iti, which = int(mi.group(1)), int(mi.group(2)), mi.group(3)
            groups.setdefault(g, {}).setdefault(iti, {})[which] = toks
        groups = {g: items[max(items)] for g, items in groups.items()}
        targets = []
        for g in sorted(groups):
            data = groups[g]
            if 'ipt' not in data:
                continue
            pts = _decode_point_array(value_tokens(data['ipt']))
            comps = _decode_component_list(data.get('ict', []))
            sd = ShapeData(aliases.get(g, "target_%d" % g))
            sd.weight = weights.get(g, 0.0)
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
            cleaned = [t for t in value_tokens(toks)
                       if t not in ('{', '}', ',')]
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
            try:
                out[sg.name] = self._material_data(mat_node)
            except Exception as exc:
                self.warnings.append("material '%s' skipped: %s"
                                     % (mat_node.name, exc))
        return out

    # base-colour attribute across material types: lambert/blinn/phong use .c,
    # Arnold aiStandardSurface and Autodesk standardSurface use .baseColor/.bc,
    # some shaders use .color / .dc. Try them in order.  Lambert's emission is
    # .ic (incandescence); .it is its transparency and .ec blinn's eccentricity.
    _COLOR_ATTRS = ('c', 'baseColor', 'bc', 'color', 'dc')
    _EMISSION_ATTRS = ('ic', 'emissionColor', 'emc')

    # Arnold's aiStandardSurface and Autodesk's standardSurface share one
    # parameter set. Each property lists its long name, then the short names
    # Maya may write into a .ma file; the first one present wins.
    _PBR_TYPES = ('aiStandardSurface', 'standardSurface')
    _PBR_ATTRS = {
        'metalness': ('metalness', 'm'),
        'roughness': ('specularRoughness', 'sr'),
        'opacity': ('opacity', 'opc', 'o'),
        'emission': ('emission', 'e'),
        'emission_color': ('emissionColor', 'emc', 'ec'),
    }
    # what both shaders use when the file stores no roughness
    _PBR_DEFAULT_ROUGHNESS = 0.2
    _NORMAL_ATTRS = ('n', 'normalCamera')

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
        trn = as_floats(node.attr_tokens('it'))
        if len(trn) >= 3 and node.ntype in ('lambert', 'blinn', 'phong',
                                            'phongE', 'anisotropic'):
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
        # a bump2d, aiBump2d or aiNormalMap between a file and the normal input
        for attr in self._NORMAL_ATTRS:
            for src, _ in self.scene.sources_of(node.name, attr):
                self._normal_source(md, self.scene.nodes.get(src))
        pbr = node.ntype in self._PBR_TYPES
        if pbr:
            self._pbr_values(md, node)
        emission = self._EMISSION_ATTRS
        if pbr:
            emission += self._PBR_ATTRS['emission_color']
        for attr in emission:
            path = self._file_path(node.name, attr)
            if path:
                md.maya_textures['emissive'] = path
                break
        if md.tex_prefix is None:
            md.tex_prefix = _texture_prefix_from_name(node.name)
        return md

    def _first_values(self, node, names, count=1):
        """Values of the first attribute in ``names`` that the file sets."""
        for name in names:
            values = as_floats(node.attr_tokens(name))
            if len(values) >= count:
                return values
        return None

    def _first_file(self, node_name, names):
        for name in names:
            path = self._file_path(node_name, name)
            if path:
                return path
        return None

    def _normal_source(self, md, src):
        if src is None:
            return
        if src.ntype == 'bump2d':
            path = self._file_path(src.name, 'bv')
            mode, depth = int(_number(src, 'bi', 0)), _number(src, 'bd', 1.0)
        elif src.ntype == 'aiNormalMap':
            path = self._first_file(src.name, ('input', 'i'))
            mode, depth = 1, 1.0
        elif src.ntype == 'aiBump2d':
            path = self._first_file(src.name, ('bumpMap', 'bm'))
            mode = 0
            depth = self._first_values(src, ('bumpHeight', 'bh'))
            depth = depth[0] if depth else 1.0
        else:
            return
        if path:
            md.maya_textures['normal'] = path
            md.normal_mode = mode
            md.bump_depth = depth

    def _pbr_values(self, md, node):
        """Metalness, roughness, opacity and emission of a standard surface."""
        attrs = self._PBR_ATTRS
        v = self._first_values(node, attrs['metalness'])
        md.metalness = v[0] if v else 0.0
        v = self._first_values(node, attrs['roughness'])
        md.roughness = v[0] if v else self._PBR_DEFAULT_ROUGHNESS
        v = self._first_values(node, attrs['opacity'], 3)
        if v:
            md.transparency = tuple(1.0 - c for c in v[:3])
        weight = self._first_values(node, attrs['emission'])
        colour = self._first_values(node, attrs['emission_color'], 3)
        mapped = self._first_file(
            node.name, self._EMISSION_ATTRS + attrs['emission_color'])
        if weight:
            md.emission_strength = weight[0]
        elif colour or mapped:
            md.emission_strength = 0.0     # authored, but Maya's weight is 0
        # else nothing authored: a map found by file name keeps full strength
        md.incandescence = (tuple(colour[:3]) if colour else
                            (1.0, 1.0, 1.0) if weight and weight[0] > 0
                            else (0.0, 0.0, 0.0))
        for slot, key in (('metallic', 'metalness'), ('roughness', 'roughness')):
            path = self._first_file(node.name, attrs[key])
            if path:
                md.maya_textures[slot] = path
        # say so when a texture sits on an input we do not read
        known = set(self._COLOR_ATTRS + self._NORMAL_ATTRS)
        for names in attrs.values():
            known.update(names)
        seen = set()
        for attr, srcs in self.scene.iter_dest_prefix(node.name, ''):
            base = attr.split('[', 1)[0]
            if base in known or base in seen:
                continue
            if any(getattr(self.scene.nodes.get(s), 'ntype', None) == 'file'
                   for s, _ in srcs):
                seen.add(base)
                self.warnings.append("material '%s': the texture on '%s' is "
                                     "not imported" % (node.name, base))

    def _file_path(self, node_name, attr):
        """Image path of the first file node feeding ``node.attr``."""
        for src, _ in self.scene.sources_of(node_name, attr):
            fnode = self.scene.nodes.get(src)
            if fnode is not None and fnode.ntype == 'file':
                ftn = fnode.attr_tokens('ftn')
                if ftn:
                    return ftn[0]
        return None

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
                d.eff_parent = effnode.parent if effnode else None
                d.end_joint = self._effector_joint(effnode, names)
            for sn, sa in self.scene.sources_of(ik.name, 'hsv'):
                d.solver = sn
            if d.start_joint in names and d.end_joint in names:
                out.append(d)
        return out

    def _effector_joint(self, effnode, names):
        """Maya parents an effector under the joint *before* the chain end and
        offsets it onto the end joint, so the end joint is the child of the
        effector's parent that the effector's offset lands on."""
        if effnode is None or effnode.parent not in names:
            return None
        parent = effnode.parent
        kids = [self.scene.nodes[n] for n in self.scene.order
                if n in names and self.scene.nodes[n].parent == parent]
        if not kids:
            return parent
        want = as_floats(effnode.attr_tokens('t'))[:3] or [0.0, 0.0, 0.0]

        def gap(k):
            t = as_floats(k.attr_tokens('t'))[:3] or [0.0, 0.0, 0.0]
            return sum((a - b) ** 2 for a, b in zip(t, want))
        return min(kids, key=gap).name

    # -- cameras --------------------------------------------------------
    def cameras(self):
        """Non-default cameras as dicts(name, transform, focal, world)."""
        defaults = {'persp', 'top', 'front', 'side', 'left', 'bottom', 'back'}
        out = []
        for cam in self.scene.of_type('camera'):
            tr = cam.parent or cam.name
            if tr in defaults:
                continue
            def num(attr, default):
                values = as_floats(cam.attr_tokens(attr))
                return values[0] if values else default
            aperture = as_floats(cam.attr_tokens('cap'))     # inches
            lin = self.scene.cm_per_unit                     # fl stays in mm
            out.append(dict(name=tr, shape=cam.name,
                            focal=num('fl', 35.0),
                            ortho=as_bool(cam.attr_tokens('o')),
                            ortho_width=num('ow', 30.0 / lin) * lin,
                            sensor_width=aperture[0] * 25.4 if aperture else None,
                            near=num('ncp', 0.1 / lin) * lin,
                            far=num('fcp', 10000.0 / lin) * lin))
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
                                spot_angle=cone[0] * self.scene.deg_per_unit
                                if cone else 40.0))
        return out

    # -- animation (time-based animCurves) ------------------------------
    _ANIM_ATTRS = {
        'tx': 'tx', 'ty': 'ty', 'tz': 'tz', 'rx': 'rx', 'ry': 'ry', 'rz': 'rz',
        'sx': 'sx', 'sy': 'sy', 'sz': 'sz',
        'translateX': 'tx', 'translateY': 'ty', 'translateZ': 'tz',
        'rotateX': 'rx', 'rotateY': 'ry', 'rotateZ': 'rz',
        'scaleX': 'sx', 'scaleY': 'sy', 'scaleZ': 'sz',
    }

    def animations(self):
        """{node name: {attr: [(frame, value), ...]}} for time-based animCurves
        driving translate / rotate / scale directly.  Values stay in Maya's
        units (centimetres, degrees); the builder converts them."""
        out = {}
        for ctype in ('animCurveTL', 'animCurveTA', 'animCurveTU', 'animCurveTT'):
            for cv in self.scene.of_type(ctype):
                keys = self._anim_keys(cv, ctype)
                if not keys:
                    continue
                for dn, da in self.scene.dests_of(cv.name, 'o'):
                    attr = self._ANIM_ATTRS.get(da)
                    if attr:
                        out.setdefault(dn, {})[attr] = keys
        return out

    def _anim_keys(self, cv, ctype):
        fl = as_floats(cv.attr_tokens('ktv'))
        # TL / TA values are in the scene's UI units; TU / TT are unitless
        k = {'animCurveTL': self.scene.cm_per_unit,
             'animCurveTA': self.scene.deg_per_unit}.get(ctype, 1.0)
        keys = [(fl[i], fl[i + 1] * k) for i in range(0, len(fl) - 1, 2)]
        keys.sort(key=lambda k: k[0])
        return keys


# ---------------------------------------------------------------------------
# small decoders / helpers
# ---------------------------------------------------------------------------
def _number(node, attr, default):
    """First number of an attribute, or ``default`` if absent or damaged."""
    values = as_floats(node.attr_tokens(attr))
    return values[0] if values and math.isfinite(values[0]) else default


def _pad3(t):
    t = list(t)
    while len(t) < 3:
        t.append(0.0)
    return tuple(t[:3])


# one token per line, so ^ means "at the start of a token"
_COMPONENT_RE = re.compile(r'^(?:vtx|cp|pt)\[(\d+)(?::(\d+))?\]', re.MULTILINE)
_FACE_RE = re.compile(r'^f\[(\d+)(?::(\d+))?\]', re.MULTILINE)


def _decode_point_array(toks):
    """Maya 'pointArray': <count> then count*(x y z w)."""
    if not toks:
        return []
    try:
        count = int(toks[0])
    except (ValueError, IndexError):
        return []
    fl = as_floats(toks[1:])
    # 4 components per point (x, y, z, w); fall back to 3 if that fits better
    stride = 4 if len(fl) >= count * 4 else 3
    return list(zip(fl[0::stride], fl[1::stride], fl[2::stride]))[:count]


def _decode_ranges(values, pattern):
    """Index ranges named by a list of components.  Undecoded text is searched
    directly (a quote becomes a line break), skipping the per-token split."""
    if isinstance(values, LazyValues):
        text = values.raw.replace('"', "\n")
    else:
        text = "\n".join(values)
    out = []
    for first, last in pattern.findall(text):
        a = int(first)
        out.extend(range(a, int(last or a) + 1))
    return out


def _decode_component_list(toks):
    """Maya 'componentList': <count> then strings like vtx[3], vtx[5:9]."""
    return _decode_ranges(toks, _COMPONENT_RE)


def _decode_face_list(toks):
    """Maya face 'componentList': <count> then f[3], f[5:9], ..."""
    return _decode_ranges(toks, _FACE_RE)


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
