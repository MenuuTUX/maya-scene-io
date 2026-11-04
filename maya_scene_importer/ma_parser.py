# SPDX-License-Identifier: GPL-3.0-or-later
"""
Maya ASCII (.ma) parser.

This module is intentionally free of any `bpy` dependency so it can be unit
tested with a plain Python interpreter.  It turns a Maya ASCII scene into a
light-weight node/connection graph that the Blender scene builder consumes.

Only the subset of MEL needed to faithfully reconstruct geometry, skeletons,
skin weights, blend shapes, materials and NURBS controls is interpreted.  Every
other command is parsed structurally (so the graph stays consistent) but
otherwise ignored.
"""

import io
import re
from collections import deque

# Tokenises a single statement into quoted strings (kept with their quotes) and
# bare words.  Implemented as one regex so the huge numeric arrays in mesh /
# weight blocks tokenise at C speed rather than char-by-char in Python.
_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[^\s"]+')
_UNESCAPE_RE = re.compile(r'\\(.)')

# `setAttr [flags] ".name" [-type "T"] values...`: everything up to the values.
# A statement that does not look like this takes the general (eager) path.
_SETATTR_RE = re.compile(
    r'setAttr\s+(?:-[A-Za-z]+(?:\s+[^\s"-][^\s"]*)?\s+)*'
    r'"(\.(?:[^"\\]|\\.)*)"')
_TYPE_RE = re.compile(r'\s*-type\s+"([^"]*)"')
_LAZY_MIN = 512      # shorter value text is decoded straight away

# currentUnit -l / -a names -> centimetres / degrees per unit.  A .ma writes
# length and angle attributes (.t, .r, .jo, clip planes, cone angles, animCurve
# values) in these UI units; data blocks (mesh points, matrices, curve CVs)
# stay in Maya's internal centimetres.
_CM_PER = {'mm': 0.1, 'millimeter': 0.1, 'cm': 1.0, 'centimeter': 1.0,
           'm': 100.0, 'meter': 100.0, 'km': 100000.0, 'kilometer': 100000.0,
           'in': 2.54, 'inch': 2.54, 'ft': 30.48, 'foot': 30.48,
           'yd': 91.44, 'yard': 91.44, 'mi': 160934.4, 'mile': 160934.4}
_DEG_PER = {'deg': 1.0, 'degree': 1.0, 'rad': 57.29577951308232,
            'radian': 57.29577951308232, 'min': 1.0 / 60, 'sec': 1.0 / 3600}


def _unescape(s):
    return _UNESCAPE_RE.sub(r'\1', s) if '\\' in s else s


class LazyValues:
    """The undecoded value text of a large ``setAttr``.

    A vertex, weight or key list can be hundreds of megabytes of numbers, and
    most of it is read once.  Keeping the text (one byte per character) instead
    of a Python string per number saves most of the memory and parse time.
    ``tokens()`` decodes on demand and keeps nothing; it is deliberately the
    only method, so code that indexes the values must decode them first."""

    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw

    def tokens(self):
        raw = self.raw
        if '"' not in raw:
            return raw.split()
        return [_unescape(m[1:-1]) if m[0] == '"' else m
                for m in _TOKEN_RE.findall(raw)]


def value_tokens(values):
    """The token list of a setAttr entry, whether decoded already or not."""
    return values.tokens() if isinstance(values, LazyValues) else values


class MNode:
    """A single Maya dependency / DAG node."""

    __slots__ = ("name", "ntype", "parent", "is_shape", "uuid",
                 "set_attrs", "add_attrs", "_attr_index", "origin")

    def __init__(self, name, ntype, parent=None, is_shape=False):
        self.name = name
        self.ntype = ntype
        self.parent = parent          # parent node name (DAG) or None
        self.is_shape = is_shape       # created with the -s flag
        self.uuid = None
        self.origin = None             # folder of the referenced file it came
                                       # from; None for the scene itself
        # list of (attr_name, type_or_None, [tokens] | LazyValues), file order
        self.set_attrs = []
        # list of dicts describing addAttr (custom attributes)
        self.add_attrs = []
        self._attr_index = None        # lazily built {base_attr: [entries]}

    # -- attribute helpers ------------------------------------------------
    def _index(self):
        if self._attr_index is None:
            idx = {}
            for name, atype, toks in self.set_attrs:
                base = name.split('[', 1)[0].split('.', 1)[0]
                idx.setdefault(base, []).append((name, atype, toks))
            self._attr_index = idx
        return self._attr_index

    def attr(self, base):
        """Return the entries (name, type, tokens) for a top level attr base."""
        return self._index().get(base, [])

    def attr_tokens(self, base):
        """Concatenate value tokens of every setAttr sharing the base name."""
        out = []
        for _, _, toks in self.attr(base):
            out.extend(value_tokens(toks))
        return out

    def has_attr(self, base):
        return base in self._index()


class MayaScene:
    def __init__(self):
        self.nodes = {}            # name -> MNode
        self.order = []            # creation order (names)
        self.connections = []      # (src_node, src_attr, dst_node, dst_attr)
        self.by_type = {}          # ntype -> [names]
        self.time_unit = 'film'    # currentUnit -t (Maya's default)
        self.linear_unit = 'cm'    # currentUnit -l
        self.angle_unit = 'deg'    # currentUnit -a
        self.play_range = None     # (min, max) from playbackOptions
        self.references = []       # paths of the files this scene references
        self.reference_info = []   # {'path', 'namespace', 'node'} for each
        self.notes = []            # messages for the user, shown as warnings
        self.skipped = 0           # statements too damaged to read
        self.paths = {}            # "|group|node" -> registered node name
        self._path_of = {}         # registered node name -> "|group|node"
        # destination attr "node.attr" -> list of source "node.attr"
        self._conn_by_dst = None
        self._conn_by_src = None
        self._dst_by_node = None    # node -> [(attr, sources)]

    @property
    def cm_per_unit(self):
        """Centimetres per scene length unit (1.0 for a .mb or a cm scene)."""
        return _CM_PER.get(self.linear_unit, 1.0)

    @property
    def deg_per_unit(self):
        """Degrees per scene angle unit (1.0 for a .mb or a degree scene)."""
        return _DEG_PER.get(self.angle_unit, 1.0)

    # -- building ---------------------------------------------------------
    def add_node(self, node):
        name = node.name
        if name in self.nodes:
            # Maya short names are not guaranteed unique across the DAG; keep
            # the first and disambiguate later ones so nothing is silently lost.
            suffix = 1
            new = "%s#%d" % (name, suffix)
            while new in self.nodes:
                suffix += 1
                new = "%s#%d" % (name, suffix)
            node.name = new
            name = new
        self.nodes[name] = node
        self.order.append(name)
        self.by_type.setdefault(node.ntype, []).append(name)

    # -- queries ----------------------------------------------------------
    def of_type(self, ntype):
        return [self.nodes[n] for n in self.by_type.get(ntype, [])]

    def _build_conn_maps(self):
        bydst, bysrc = {}, {}
        for sn, sa, dn, da in self.connections:
            bydst.setdefault("%s.%s" % (dn, da), []).append((sn, sa))
            bysrc.setdefault("%s.%s" % (sn, sa), []).append((dn, da))
        self._conn_by_dst, self._conn_by_src = bydst, bysrc
        by_node = {}
        for key, srcs in bydst.items():
            node, _, attr = key.partition('.')
            by_node.setdefault(node, []).append((attr, srcs))
        self._dst_by_node = by_node

    def sources_of(self, dst_node, dst_attr):
        """Who feeds <dst_node>.<dst_attr>?  Returns list of (node, attr)."""
        if self._conn_by_dst is None:
            self._build_conn_maps()
        return self._conn_by_dst.get("%s.%s" % (dst_node, dst_attr), [])

    def dests_of(self, src_node, src_attr):
        if self._conn_by_src is None:
            self._build_conn_maps()
        return self._conn_by_src.get("%s.%s" % (src_node, src_attr), [])

    def iter_dest_prefix(self, dst_node, attr_prefix):
        """Yield (dst_attr, [(src_node, src_attr)]) for connections whose
        destination is on <dst_node> and whose attr starts with attr_prefix."""
        if self._dst_by_node is None:
            self._build_conn_maps()
        for attr, srcs in self._dst_by_node.get(dst_node, ()):
            if attr.startswith(attr_prefix):
                yield attr, srcs


# ----------------------------------------------------------------------------
# statement splitting
# ----------------------------------------------------------------------------
_MAX_OPEN_STRING = 8000000   # a quote left open this long is a stray, not a string


def _odd_quotes(fragment):
    """True when ``fragment`` leaves a double-quoted string open."""
    return (fragment.count('"') - fragment.count('\\"')) % 2 == 1


def _iter_statements(lines):
    """Yield every statement in ``lines``; see :func:`_scan`.  A string still
    open at the end of the file means a stray quote earlier on: that one
    statement is dropped and the text after it is read again."""
    while True:
        leftover = yield from _scan(lines)
        if leftover is None:
            return
        lines = io.StringIO(leftover)


def _scan(lines):
    """Yield raw statement strings (without the trailing ';') from an iterable
    of text lines, holding only the statement being assembled in memory.

    Comment lines (starting with //) are dropped first.  Statements are split
    on ';' and re-merged while a fragment leaves a double-quoted string open,
    which repairs the rare ';' that lives inside a quoted MEL string (e.g.
    inside `script` nodes).  A quote that stays open for megabytes is a stray
    one in a damaged file, so that statement is yielded as it is and reading
    resumes at the next one instead of swallowing the rest of the file."""
    chunks = []
    open_parts = []              # fragments of a statement whose string is open
    open_size = 0
    for line in lines:
        if line.lstrip().startswith('//'):
            continue
        chunks.append(line)
        if ';' not in line:
            continue                  # still inside a long multi-line statement
        parts = ''.join(chunks).split(';')
        chunks = [parts.pop()]
        work = deque(parts)
        while work:
            frag = work.popleft()
            if not open_parts:
                if _odd_quotes(frag):
                    open_parts, open_size = [frag], len(frag)
                    continue
                frag = frag.strip()
                if frag:
                    yield frag
                continue
            open_parts.append(frag)
            open_size += len(frag)
            if _odd_quotes(frag):                 # this fragment closes it
                stmt = ';'.join(open_parts).strip()
                open_parts, open_size = [], 0
                if stmt:
                    yield stmt
            elif open_size > _MAX_OPEN_STRING:
                first, rest = open_parts[0].strip(), open_parts[1:]
                open_parts, open_size = [], 0
                work.extendleft(reversed(rest))   # read the rest again, fresh
                if first:
                    yield first
    tail = ''.join(chunks)
    if open_parts:
        first = open_parts[0].strip()
        if first:
            yield first
        return ';'.join(open_parts[1:] + [tail])      # to be read again
    tail = tail.strip()
    if tail:
        yield tail


def _tok(statement):
    """Tokenise into a list of (kind, value): kind is 'str' or 'word'."""
    return [('str', _unescape(m[1:-1])) if m[0] == '"' else ('word', m)
            for m in _TOKEN_RE.findall(statement)]


# ----------------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------------
class MAParser:
    def __init__(self):
        self.scene = MayaScene()
        self._cur = None         # current node (target of setAttr/addAttr)
        self._select = None      # node named by an explicit `select`

    def parse_file(self, path):
        with open(path, 'rb') as fh:
            if fh.read(4) in (b'FOR4', b'FOR8'):
                raise ValueError("this is a binary Maya scene with a .ma name, "
                                 "not Maya ASCII text")
        # streamed line by line, so a huge scene never exists as one string
        try:
            with open(path, 'r', encoding='utf-8-sig') as fh:
                return self._parse_lines(fh)
        except UnicodeDecodeError:
            # older Maya on Windows saved in the system code page
            self.__init__()
            with open(path, 'r', encoding='cp1252', errors='replace') as fh:
                return self._parse_lines(fh)

    def _parse_lines(self, lines):
        for stmt in _iter_statements(lines):
            if stmt.startswith('setAttr') and self._set_attr_fast(stmt):
                continue
            toks = _tok(stmt)
            if not toks:
                continue
            cmd = toks[0][1] if toks[0][0] == 'word' else None
            handler = self._dispatch.get(cmd)
            if handler:
                try:
                    handler(self, toks[1:])
                except (IndexError, ValueError):
                    self.scene.skipped += 1      # damaged or truncated statement
        return self.scene

    # -- individual commands ---------------------------------------------
    def _c_createNode(self, args):
        ntype = None
        name = None
        parent = None
        is_shape = False
        i = 0
        while i < len(args):
            kind, val = args[i]
            if kind == 'word' and val == '-n':
                name = args[i + 1][1]; i += 2; continue
            if kind == 'word' and val == '-p':
                parent = args[i + 1][1]; i += 2; continue
            if kind == 'word' and val == '-s':
                is_shape = True; i += 1; continue
            if kind == 'word' and val in ('-ss', '-shared'):
                i += 1; continue
            if ntype is None and kind == 'word' and not val.startswith('-'):
                ntype = val
            i += 1
        if name is None:
            name = "%s#anon%d" % (ntype or 'node', len(self.scene.order))
        parent = self._resolve(parent)
        node = MNode(name, ntype or 'unknown', parent, is_shape)
        self.scene.add_node(node)
        # Maya names a node by its |full|path when its short name is shared
        parent_path = self.scene._path_of.get(parent, '|' + parent) \
            if parent else ''
        path = "%s|%s" % (parent_path, name)
        self.scene.paths[path] = node.name
        self.scene._path_of[node.name] = path
        self._cur = node
        self._select = None

    def _resolve(self, ref):
        """The registered name of a node reference: a short name stays as it
        is, a ``|full|path`` finds the node that was created there."""
        if ref and ref.startswith('|'):
            return self.scene.paths.get(ref) or ref.rsplit('|', 1)[-1]
        return ref

    def _c_rename(self, args):
        # `rename -uid "..."` targets the most recent node
        for i, (kind, val) in enumerate(args):
            if kind == 'word' and val == '-uid' and i + 1 < len(args):
                if self._cur is not None:
                    self._cur.uuid = args[i + 1][1]
                return

    def _target(self):
        return self._select or self._cur

    def _set_attr_fast(self, stmt):
        """Record a plain ``setAttr`` without tokenising its values.  Returns
        False, having changed nothing, for anything unusual."""
        m = _SETATTR_RE.match(stmt)
        if m is None:
            return False
        node = self._target()
        if node is None:
            return True               # nothing to attach it to, as before
        rest = stmt[m.end():]
        tm = _TYPE_RE.match(rest)
        atype = None
        if tm:
            atype = tm.group(1)
            rest = rest[tm.end():]
        if '-type' in rest:
            return False
        if len(rest) < _LAZY_MIN:
            values = LazyValues(rest).tokens()
        else:
            values = LazyValues(rest) if rest.strip() else []
        node.set_attrs.append((_unescape(m.group(1)).lstrip('.'), atype, values))
        return True

    def _c_setAttr(self, args):
        node = self._target()
        if node is None:
            return
        # The attribute name is the first quoted token beginning with '.', e.g.
        # ".vt[0:165]".  Flags (-s, -k, -l, ...) may precede it and, crucially,
        # `-type "T"` may appear *after* it (Maya writes
        # `setAttr ".fc[0:499]" -type "polyFaces" ...`).  Everything after the
        # name that is not the -type pair is a value token.
        name_idx = None
        for idx, (kind, val) in enumerate(args):
            if kind == 'str' and val.startswith('.'):
                name_idx = idx
                break
        if name_idx is None:
            return
        attr_name = args[name_idx][1].lstrip('.')
        atype = None
        values = []
        i = name_idx + 1
        n = len(args)
        while i < n:
            kind, val = args[i]
            if kind == 'word' and val == '-type' and i + 1 < n:
                atype = args[i + 1][1]
                i += 2
                continue
            values.append(val)
            i += 1
        node.set_attrs.append((attr_name, atype, values))

    def _c_addAttr(self, args):
        node = self._target()
        if node is None:
            return
        info = {}
        i = 0
        n = len(args)
        flagmap = {
            '-ln': 'ln', '-longName': 'ln',
            '-sn': 'sn', '-shortName': 'sn',
            '-at': 'at', '-attributeType': 'at',
            '-dt': 'dt', '-dataType': 'dt',
            '-dv': 'dv', '-defaultValue': 'dv',
            '-min': 'min', '-minValue': 'min',
            '-max': 'max', '-maxValue': 'max',
            '-en': 'en', '-enumName': 'en',
            '-p': 'parent', '-parent': 'parent',
        }
        while i < n:
            kind, val = args[i]
            if kind == 'word' and val in flagmap and i + 1 < n:
                info[flagmap[val]] = args[i + 1][1]
                i += 2
                continue
            i += 1
        if 'ln' in info or 'sn' in info:
            node.add_attrs.append(info)

    def _c_connectAttr(self, args):
        # connectAttr "src.attr" "dst.attr" [flags]
        strs = [v for (k, v) in args if k == 'str']
        if len(strs) < 2:
            return
        src, dst = strs[0], strs[1]
        sn, _, sa = src.partition('.')
        dn, _, da = dst.partition('.')
        self.scene.connections.append(
            (self._resolve(sn), sa, self._resolve(dn), da))

    def _c_select(self, args):
        # `select -ne "node"` or `select "node"` — sets setAttr target
        name = None
        for kind, val in args:
            if kind == 'str':
                name = val
            elif kind == 'word' and not val.startswith('-'):
                name = val
        name = self._resolve(name)
        if name and name in self.scene.nodes:
            self._select = self.scene.nodes[name]

    def _c_parent(self, args):
        # reparent shapes: `parent -s -nc -r -add "shape" "transform"` etc.
        # Track only the simple `parent "child" "parent"` DAG case so shapes are
        # not orphaned; ignored when flags make it ambiguous.
        strs = [v for (k, v) in args if k == 'str']
        if len(strs) == 2:
            child, newpar = self._resolve(strs[0]), self._resolve(strs[1])
            if child in self.scene.nodes:
                self.scene.nodes[child].parent = newpar

    def _c_file(self, args):
        # `file -r -ns "ns" -rfn "nsRN" -typ "mayaAscii" "path"` pulls in
        # another scene. Maya writes a `file -rdi` line first that only repeats
        # the depth of the same reference, so only `-r` counts.
        flags = {val for kind, val in args if kind == 'word'}
        paths = [val for kind, val in args if kind == 'str']
        if not paths or not flags & {'-r', '-reference'}:
            return
        info = {'path': paths[-1], 'namespace': None, 'node': None}
        for (kind, val), (_, nxt) in zip(args, args[1:]):
            if kind != 'word':
                continue
            if val in ('-ns', '-namespace'):
                info['namespace'] = nxt
            elif val in ('-rfn', '-referenceNode'):
                info['node'] = nxt
        key = (info['namespace'], info['path'])
        if key not in {(i['namespace'], i['path'])
                       for i in self.scene.reference_info}:
            self.scene.references.append(info['path'])
            self.scene.reference_info.append(info)

    def _c_currentUnit(self, args):
        for i, (kind, val) in enumerate(args[:-1]):
            if kind == 'word' and val in ('-t', '-time'):
                self.scene.time_unit = args[i + 1][1]
            elif kind == 'word' and val in ('-l', '-linear'):
                self.scene.linear_unit = args[i + 1][1]
            elif kind == 'word' and val in ('-a', '-angle'):
                self.scene.angle_unit = args[i + 1][1]

    def _c_playbackOptions(self, args):
        lo = hi = None
        for i, (kind, val) in enumerate(args[:-1]):
            if kind != 'word':
                continue
            try:
                if val in ('-min', '-minTime'):
                    lo = float(args[i + 1][1])
                elif val in ('-max', '-maxTime'):
                    hi = float(args[i + 1][1])
            except ValueError:
                pass
        if lo is not None and hi is not None:
            self.scene.play_range = (lo, hi)

    _dispatch = {
        'file': _c_file,
        'currentUnit': _c_currentUnit,
        'playbackOptions': _c_playbackOptions,
        'createNode': _c_createNode,
        'rename': _c_rename,
        'setAttr': _c_setAttr,
        'addAttr': _c_addAttr,
        'connectAttr': _c_connectAttr,
        'select': _c_select,
        'parent': _c_parent,
    }


# ----------------------------------------------------------------------------
# value decoding helpers (used by the builder)
# ----------------------------------------------------------------------------
def as_floats(tokens):
    """Numbers in ``tokens``; anything that is not a number is skipped."""
    try:
        return list(map(float, tokens))
    except ValueError:
        out = []
        for t in tokens:
            try:
                out.append(float(t))
            except ValueError:
                pass
        return out


def as_bool(tokens, default=False):
    if not tokens:
        return default
    t = tokens[0].lower()
    return t in ('1', 'yes', 'true', 'on')


def parse_file(path):
    return MAParser().parse_file(path)


# ----------------------------------------------------------------------------
# stand-alone diagnostics:  python3 ma_parser.py scene.ma
# ----------------------------------------------------------------------------
if __name__ == '__main__':
    import sys
    import time
    t0 = time.time()
    scene = parse_file(sys.argv[1])
    dt = time.time() - t0
    print("parsed %d nodes / %d connections in %.2fs" %
          (len(scene.nodes), len(scene.connections), dt))
    counts = sorted(((len(v), k) for k, v in scene.by_type.items()), reverse=True)
    for c, k in counts[:25]:
        print("  %5d  %s" % (c, k))
