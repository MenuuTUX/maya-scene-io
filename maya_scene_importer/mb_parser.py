# SPDX-License-Identifier: GPL-3.0-or-later
"""
Maya Binary (.mb) reader that needs no Maya.

A `.mb` file is an IFF tree: Maya 2014 and later write 64-bit ``FOR8`` chunks,
older and 32-bit Maya write ``FOR4``.  Every node is a group tagged with a
four-letter type code (``XFRM`` transform, ``DMSH`` mesh, ...) holding a
``CREA`` chunk (name, parent) and one chunk per attribute value.

This reader fills the same :class:`ma_parser.MayaScene` as the `.ma` parser,
with attribute values turned into the tokens the `.ma` text would have, so the
model and the scene builder need no `.mb` knowledge.  Values a `.mb` stores
differently are converted on the way: angles from radians to degrees, key times
from ticks to frames, and mesh data from Maya's packed polygon record.

Like :mod:`ma_parser`, this module has no ``bpy`` dependency.
"""

import re
import struct

try:
    from .ma_parser import MAParser
except ImportError:                 # plain-Python use (tests, diagnostics)
    from ma_parser import MAParser


# Type codes read off real files (Maya 2009, 2014 and 2020).  Codes for other
# types are not guessed; those nodes fall back to their default Maya name
# (file1, phong2, spotLightShape1, ...) or to their attributes.
_TYPES = {
    b'XFRM': 'transform', b'DMSH': 'mesh', b'JOIN': 'joint',
    b'LOCT': 'locator', b'DCAM': 'camera', b'DCAV': 'cameraView',
    b'NCRV': 'nurbsCurve', b'NCRC': 'makeNurbCircle',
    b'FSCL': 'skinCluster', b'FMPT': 'tweak', b'FPOS': 'dagPose',
    b'GRPP': 'groupParts', b'GPID': 'groupId', b'OBST': 'objectSet',
    b'SHAD': 'shadingEngine', b'DMTI': 'materialInfo',
    b'RLAM': 'lambert', b'RBLN': 'blinn',
    b'DSPL': 'displayLayer', b'DPLM': 'displayLayerManager',
    b'RNDL': 'renderLayer', b'RNLM': 'renderLayerManager',
    b'RLLK': 'lightLinker', b'SCRP': 'script', b'SQNC': 'sequencer',
    b'PCTL': 'animCurveTL', b'PCTA': 'animCurveTA', b'PCTU': 'animCurveTU',
    b'PCTT': 'animCurveTT',
    b'PCUB': 'polyCube', b'TGEO': 'transformGeometry',
    b'RPLD': 'place3dTexture', b'RTGR': 'granite',
    b'DDMN': 'distanceDimShape', b'PSDM': 'poseInterpolatorManager',
    b'SDML': 'shapeEditorManager',
}

# types the importer uses, recognised from a default node name when their
# code is not in _TYPES ("spotLightShape2" -> spotLight)
_NAMED_TYPES = (
    'place2dTexture', 'file', 'bump2d', 'phongE', 'phong', 'anisotropic',
    'surfaceShader', 'blendShape', 'ikHandle', 'ikEffector', 'ikRPsolver',
    'ikSCsolver', 'directionalLight', 'pointLight', 'spotLight', 'areaLight',
    'ambientLight', 'volumeLight', 'nurbsSurface', 'aiStandardSurface',
    'standardSurface',
)
_NAME_RE = re.compile(r'^(?:.*:)?([A-Za-z]+?)(?:Shape)?\d*$')

_ANGLE_ATTRS = {'r', 'rx', 'ry', 'rz', 'ra', 'rax', 'ray', 'raz',
                'jo', 'jox', 'joy', 'joz'}
_LIGHT_ANGLES = {'ca', 'pa'}             # spot cone and penumbra
_DEG = 57.29577951308232

_FPS = {'game': 15, 'film': 24, 'pal': 25, 'ntsc': 30, 'show': 48,
        'palf': 50, 'ntscf': 60}

_COMPONENTS = {b'CMDF': 'f', b'CMDV': 'vtx', b'CMDE': 'e'}

_GROUPS = {8: (b'FOR8', b'LIS8', b'CAT8', b'PRO8'),
           4: (b'FOR4', b'LIS4', b'CAT4', b'PRO4',
               b'FORM', b'LIST', b'CAT ', b'PROP')}


def _num(x):
    """A number as the token Maya ASCII would write (1.0 -> '1')."""
    if abs(x) < 1e15 and x == int(x):         # nan and inf fail the first test
        return '%d' % x
    return repr(x)


def _cstr(buf, pos):
    """The NUL-terminated string at ``pos`` and the offset after it."""
    end = buf.find(b'\0', pos)
    if end < 0:
        end = len(buf)
    return buf[pos:end].decode('utf-8', 'replace'), end + 1


class MBReader:
    def __init__(self, data):
        self.data = data
        self.wide = data[:4] == b'FOR8'
        self.head = 16 if self.wide else 8
        self.align = 8 if self.wide else 4
        self.groups = _GROUPS[8 if self.wide else 4]
        self.ma = MAParser()                 # node naming / paths / connections
        self.scene = self.ma.scene
        self.scene.notes = []
        self.version = 0
        self.fps = 24.0
        self.unknown = {}
        self.odd_faces = 0
        self.extra_mesh_data = 0
        self.bad_meshes = []

    # -- IFF walking ------------------------------------------------------
    def children(self, off, end):
        """Yield (tag, group_type_or_None, body_start, body_end)."""
        data, head, align = self.data, self.head, self.align
        while off + head <= end:
            tag = data[off:off + 4]
            if self.wide:
                size = struct.unpack_from('>Q', data, off + 8)[0]
            else:
                size = struct.unpack_from('>I', data, off + 4)[0]
            body = off + head
            if body + size > end:
                self.scene.skipped += 1          # truncated file
                return
            if tag in self.groups:
                yield tag, data[body:body + 4], body + 4, body + size
                # a 64-bit group's size is already aligned; a 32-bit one pads
                off = body + (size if self.wide else (size + 3) & ~3)
            else:
                yield tag, None, body, body + size
                off = body + ((size + align - 1) & ~(align - 1))

    def read(self):
        data = self.data
        if data[:4] not in (b'FOR8', b'FOR4') or data[self.head:self.head + 4] \
                != b'Maya':
            raise ValueError("not a Maya Binary scene")
        start = self.head + 4
        for tag, gtype, b, e in self.children(start, len(data)):
            if gtype is None:
                continue
            try:
                if gtype == b'HEAD':
                    self._header(b, e)
                elif gtype in (b'CONS', b'CONN'):
                    self._connections(b, e)
                elif gtype == b'SLCT':
                    self._select(b, e)
                else:
                    self._node(gtype, b, e)
            except (struct.error, IndexError, ValueError, OverflowError):
                self.scene.skipped += 1
        self._notes()
        return self.scene

    # -- header -----------------------------------------------------------
    def _header(self, b, e):
        for tag, _, cb, ce in self.children(b, e):
            text = self.data[cb:ce].split(b'\0')[0].decode('utf-8', 'replace')
            if tag == b'VERS':
                m = re.match(r'(\d+)', text)
                self.version = int(m.group(1)) if m else 0
            elif tag == b'TUNI' and text:
                self.scene.time_unit = text
                m = re.match(r'([\d.]+)fps$', text)
                self.fps = float(_FPS.get(text) or (m.group(1) if m else 24))
            elif tag in (b'FREF', b'RFRN'):
                parts = [p for p in self.data[cb:ce].split(b'\0') if p]
                if parts:
                    self.scene.references.append(
                        parts[-1].decode('utf-8', 'replace'))

    @property
    def has_uuid(self):
        return self.version >= 2016

    @property
    def tick_rate(self):
        # Maya 2019 moved its internal time from 6000 to 141120000 ticks a second
        return 141120000.0 if self.version >= 2019 else 6000.0

    # -- connections ------------------------------------------------------
    def _connections(self, b, e):
        for tag, gtype, cb, ce in self.children(b, e):
            if gtype is not None:
                self._connections(cb, ce)
            elif tag == b'CWFL':
                buf = self.data[cb:ce]
                src, pos = _cstr(buf, 1)
                dst, _ = _cstr(buf, pos)
                if src and dst:
                    self.ma._c_connectAttr([('str', src.lstrip(':')),
                                            ('str', dst.lstrip(':'))])

    def _select(self, b, e):
        """``select -ne node`` followed by setAttrs on an existing node."""
        node = None
        for tag, gtype, cb, ce in self.children(b, e):
            if tag == b'SLCT' and gtype is None:
                name = self.data[cb:ce].split(b'\0')[0].decode('utf-8', 'replace')
                name = self.ma._resolve(name.lstrip(':'))
                node = self.scene.nodes.get(name)
                if node is None and name == 'time1':
                    self._time(b, e)
                    return
            elif node is not None and gtype is None:
                self._attr(node, tag, self.data[cb:ce])

    def _time(self, b, e):
        """time1.outTime, in ticks: the frame the scene was saved on."""
        for tag, gtype, cb, ce in self.children(b, e):
            buf = self.data[cb:ce]
            if tag == b'DBLE' and buf.startswith(b'o\0'):
                p = buf[3:]
                fmt = {4: '>i', 8: '>q'}.get(len(p))
                if fmt:
                    ticks = struct.unpack(fmt, p)[0]
                    self.scene.current_frame = ticks / (self.tick_rate /
                                                        self.fps)

    # -- nodes ------------------------------------------------------------
    def _node(self, code, b, e):
        chunks = [(tag, self.data[cb:ce])
                  for tag, gtype, cb, ce in self.children(b, e) if gtype is None]
        if not chunks or chunks[0][0] != b'CREA':
            return
        flags, name, parent = self._crea(chunks[0][1])
        attrs = [c for c in chunks[1:] if c[0] != b'ATTR']
        ntype = self._type(code, name, attrs)
        args = [('word', ntype), ('word', '-n'), ('str', name)]
        if parent:
            args += [('word', '-p'), ('str', parent)]
        if flags & 1:
            args.append(('word', '-s'))
        self.ma._c_createNode(args)
        node = self.ma._cur
        self._mesh_result = None
        for tag, buf in chunks[1:]:
            if tag == b'ATTR':
                self._add_attr(node, buf)
            else:
                self._attr(node, tag, buf)
        if self._mesh_result is not None:
            # the MESH record is the one source of geometry and UVs, and an
            # outMesh ('o') already has the vertex tweaks applied
            attr, entries = self._mesh_result
            node.set_attrs[:] = [
                a for a in node.set_attrs
                if not (a[0].startswith('uvst[') or
                        (attr == 'o' and a[0].startswith('pt[')))] + entries
            node._attr_index = None
        if ntype == 'script' and 'sceneConfiguration' in name:
            self._play_range(node)

    def _crea(self, buf):
        flags = buf[0]
        body = buf[1:-16] if self.has_uuid and len(buf) > 17 else buf[1:]
        name, pos = _cstr(body, 0)
        parent = _cstr(body, pos)[0] if pos < len(body) else None
        return flags, name, parent or None

    def _type(self, code, name, attrs):
        ntype = _TYPES.get(code)
        if ntype:
            return ntype
        m = _NAME_RE.match(name)
        if m and m.group(1) in _NAMED_TYPES:
            return m.group(1)
        names = {buf.split(b'\0', 1)[0] for _, buf in attrs}
        if b'ftn' in names:
            return 'file'
        if code[:1].isalpha():
            self.unknown[code] = self.unknown.get(code, 0) + 1
            return 'unknown_' + code.decode('latin1').strip()
        return 'plugin_%s' % code.hex()        # a plugin's numeric type id

    def _add_attr(self, node, buf):
        # aXXX <flags> 00000001 <4 bytes> longName\0 shortName\0 parent\0
        k = buf.find(b'\0\0\0\x01', 4)
        if k < 0:
            return
        ln, pos = _cstr(buf, k + 8)
        sn, pos = _cstr(buf, pos)
        info = {'ln': ln, 'sn': sn}
        parent = _cstr(buf, pos)[0] if pos < len(buf) else ''
        if parent:
            info['parent'] = parent
        if ln:
            node.add_attrs.append(info)

    # -- attribute values -------------------------------------------------
    def _attr(self, node, tag, buf):
        z = buf.find(b'\0')
        if z <= 0:
            return
        name = buf[:z].decode('utf-8', 'replace')
        p = buf[z + 2:]                     # skip the flag byte after the name
        values = self._values(node, tag, name, p)
        if values is not None:
            node.set_attrs.append((name, None, values))

    def _values(self, node, tag, name, p):
        t = tag[:3]
        if tag == b'MESH':
            self._mesh(node, name, p)
            return None
        if tag == b'CMPD':
            return self._keys(node, name, p) if name.startswith('ktv') else None
        if tag == b'CMP#':
            return self._components(p)
        if tag == b'NRBC':
            return self._nurbs_curve(p)
        if tag == b'MATR':
            return [_num(v) for v in struct.unpack('>16d', p)] \
                if len(p) == 128 else None
        if tag == b'STR ':
            return [p.split(b'\0')[0].decode('utf-8', 'replace')]
        if tag == b'STR#':
            n = struct.unpack_from('>I', p)[0]
            return [s.decode('utf-8', 'replace')
                    for s in p[4:].split(b'\0')[:n]]
        if tag in (b'I32#', b'DBL#', b'FLT#'):
            fmt = {b'I32#': 'i', b'DBL#': 'd', b'FLT#': 'f'}[tag]
            n = struct.unpack_from('>I', p)[0]
            vals = struct.unpack_from('>%d%s' % (n, fmt), p, 4)
            return [str(n)] + [_num(v) for v in vals]
        if t == b'DBL' and name.startswith('wl[') and name.endswith('.w'):
            return self._weights(p)
        size, fmt = {b'DBL': (8, 'd'), b'FLT': (4, 'f'), b'INT': (4, 'i'),
                     b'SHR': (2, 'h'), b'BYT': (1, 'b')}.get(t, (0, None))
        if fmt is None:
            return None                     # FLGS and unknown chunk types
        if len(p) % size:
            if t == b'DBL' and len(p) % 4 == 0:
                size, fmt = 4, 'i'          # a time value in ticks
            else:
                return None
        vals = struct.unpack('>%d%s' % (len(p) // size, fmt), p)
        base = name.split('[', 1)[0].split('.', 1)[0]
        if base in _ANGLE_ATTRS or (base in _LIGHT_ANGLES and
                                    node.ntype.endswith('Light')):
            vals = [v * _DEG for v in vals]
        if len(vals) > 64:
            return list(vals)               # big arrays stay numbers
        return [_num(v) for v in vals]

    def _weights(self, p):
        """Sparse skin weights: per vertex a count, then (index, weight)s."""
        out = []
        o, n = 0, len(p)
        while o + 4 <= n:
            cnt = struct.unpack_from('>I', p, o)[0]; o += 4
            out.append(cnt)
            for _ in range(cnt):
                idx, w = struct.unpack_from('>Id', p, o); o += 12
                out.append(idx); out.append(w)
        return out

    def _keys(self, node, name, p):
        """animCurve ktv: (time, value) pairs.  Time-input curves store the
        time in ticks (int32 before Maya 2019, int64 after); unitless-input
        curves store a double."""
        m = re.search(r'\[(\d+)(?::(\d+))?\]', name)
        if not m:
            return None
        count = int(m.group(2) or m.group(1)) - int(m.group(1)) + 1
        if count <= 0 or len(p) % count:
            return None
        step = len(p) // count
        timed = node.ntype.startswith('animCurveT')
        if timed and step == 12:
            fmt = '>id'
        elif timed and step == 16:
            fmt = '>qd'
        elif step == 16:
            fmt = '>dd'
        else:
            return None
        per_frame = self.tick_rate / self.fps
        scale = _DEG if node.ntype == 'animCurveTA' else 1.0
        out = []
        for i in range(count):
            t, v = struct.unpack_from(fmt, p, i * step)
            out.append(_num(t / per_frame) if timed else _num(t))
            out.append(_num(v * scale))
        return out

    def _components(self, p):
        n = struct.unpack_from('>I', p)[0]
        o, out = 4, []
        for _ in range(n):
            kind = _COMPONENTS.get(p[o:o + 4]); o += 4
            k = struct.unpack_from('>I', p, o)[0]; o += 4
            pairs = struct.unpack_from('>%di' % (2 * k), p, o); o += 8 * k
            if kind is None:
                continue
            for a, b in zip(pairs[0::2], pairs[1::2]):
                out.append('%s[*]' % kind if a < 0 else
                           '%s[%d]' % (kind, a) if a == b else
                           '%s[%d:%d]' % (kind, a, b))
        return [str(len(out))] + out

    def _nurbs_curve(self, p):
        """The .ma nurbsCurve tokens: degree spans form rational dim, knots, CVs."""
        degree, spans, form = struct.unpack_from('>3I', p)
        rational = p[12]
        dim, nk = struct.unpack_from('>2I', p, 13)
        o = 21
        knots = struct.unpack_from('>%dd' % nk, p, o); o += 8 * nk
        ncv = struct.unpack_from('>I', p, o)[0]; o += 4
        stride = dim + (1 if rational else 0)
        cvs = struct.unpack_from('>%dd' % (ncv * stride), p, o)
        return ([str(degree), str(spans), str(form),
                 'yes' if rational else 'no', str(dim), str(nk)]
                + [_num(k) for k in knots] + [str(ncv)] + [_num(c) for c in cvs])

    # -- meshes -----------------------------------------------------------
    def _mesh(self, node, attr, p):
        """Maya's packed polygon record -> the .ma vt / ed / fc / uvst values.

        Layout: vertex floats, edge pairs (bit 31 of the first = hard edge),
        face-edges (bit 31 = reversed, bits 30-29 both set = last of a face),
        a per-face-vertex normal block (with one more count after it), then
        per UV set a name, the UV floats and a UV index per face-vertex."""
        o = 0

        def u32():
            nonlocal o
            v = struct.unpack_from('>I', p, o)[0]
            o += 4
            return v

        nv = u32()
        vt = struct.unpack_from('>%df' % nv, p, o); o += 4 * nv
        ne = u32()
        eraw = struct.unpack_from('>%dI' % ne, p, o); o += 4 * ne
        nfe = u32()
        fraw = struct.unpack_from('>%dI' % nfe, p, o); o += 4 * nfe
        nn = u32()
        o += 4 * nn                          # normals: not imported
        if nn:
            extra = u32()                    # follows the normals; 0 so far
            o += 4 * extra
        uvsets = []
        for _ in range(u32()):
            o += 4
            end = p.index(b'\0', o)
            uvname = p[o:end].decode('utf-8', 'replace'); o = end + 1
            nuv = u32()
            uvs = struct.unpack_from('>%df' % nuv, p, o); o += 4 * nuv
            nfu = u32()
            fuv = struct.unpack_from('>%di' % nfu, p, o); o += 4 * nfu
            uvsets.append((uvname, uvs, fuv if nfu == nfe else None))
        if any(p[o:]):
            self.extra_mesh_data += 1        # colour sets / creases: not read
        # a layout this reader does not know shows up as indices out of range
        if (ne and max(x & 0x7fffffff for x in eraw) * 3 >= nv) or \
                (nfe and max(x & 0x1fffffff for x in fraw) * 2 >= ne) or \
                any(fuv and max(fuv) * 2 >= len(uvs)
                    for _, uvs, fuv in uvsets):
            self.bad_meshes.append(node.name)
            return

        ed = []
        for i in range(0, ne - 1, 2):
            a = eraw[i]
            ed.append(a & 0x7fffffff)
            ed.append(eraw[i + 1] & 0x7fffffff)
            ed.append('0' if a >> 31 else '1')
        fc = []
        start = 0
        for i, x in enumerate(fraw):
            if (x >> 29) & 3 not in (0, 3):
                self.odd_faces += 1
            if (x >> 29) & 3 != 3:
                continue
            loop = fraw[start:i + 1]
            fc.append('f')
            fc.append(len(loop))
            for y in loop:
                e = y & 0x1fffffff
                fc.append(-e - 1 if y >> 31 else e)
            for s, (_, _, fuv) in enumerate(uvsets):
                if fuv is None:
                    continue
                ids = fuv[start:i + 1]
                if min(ids) < 0:
                    continue                 # face without UVs in this set
                fc.append('mu')
                fc.append(s)
                fc.append(len(ids))
                fc.extend(ids)
            start = i + 1

        entries = [('vt', None, list(vt)), ('ed', None, ed), ('fc', None, fc)]
        for s, (uvname, uvs, _) in enumerate(uvsets):
            entries.append(('uvst[%d].uvsn' % s, None, [uvname]))
            entries.append(('uvst[%d].uvsp' % s, None, list(uvs)))
        self._mesh_result = (attr, entries)

    # -- misc -------------------------------------------------------------
    def _play_range(self, node):
        text = ' '.join(str(t) for t in node.attr_tokens('b'))
        lo = re.search(r'-min(?:Time)?\s+(-?[\d.]+)', text)
        hi = re.search(r'-max(?:Time)?\s+(-?[\d.]+)', text)
        if lo and hi:
            self.scene.play_range = (float(lo.group(1)), float(hi.group(1)))

    def _notes(self):
        notes = self.scene.notes
        if self.unknown:
            notes.append(
                "%d node(s) of %d Maya Binary type(s) this reader does not "
                "know were read without a type (%s)" % (
                    sum(self.unknown.values()), len(self.unknown),
                    ", ".join(sorted(c.decode('latin1')
                                     for c in self.unknown))))
        if self.odd_faces:
            notes.append("%d face edge(s) with unknown flags (holes?) were "
                         "read as plain edges" % self.odd_faces)
        if self.bad_meshes:
            notes.append("%d mesh(es) use a .mb mesh layout this reader does "
                         "not recognise and were skipped (%s); the Maya Bridge "
                         "can read them" % (len(self.bad_meshes),
                                            ", ".join(self.bad_meshes[:5])))
        if self.extra_mesh_data:
            notes.append("%d mesh(es) carry colour sets or creases, which are "
                         "not read from .mb yet" % self.extra_mesh_data)


def parse_file(path):
    with open(path, 'rb') as fh:
        data = fh.read()
    return MBReader(data).read()


def is_binary(path):
    """True when the file is a Maya Binary scene, whatever its extension."""
    try:
        with open(path, 'rb') as fh:
            return fh.read(4) in (b'FOR4', b'FOR8')
    except OSError:
        return False


# ----------------------------------------------------------------------------
# stand-alone diagnostics:  python3 mb_parser.py scene.mb
# ----------------------------------------------------------------------------
if __name__ == '__main__':
    import sys
    import time
    t0 = time.time()
    scene = parse_file(sys.argv[1])
    print("parsed %d nodes / %d connections in %.2fs" %
          (len(scene.nodes), len(scene.connections), time.time() - t0))
    counts = sorted(((len(v), k) for k, v in scene.by_type.items()),
                    reverse=True)
    for c, k in counts[:25]:
        print("  %5d  %s" % (c, k))
    for n in scene.notes:
        print("note:", n)
