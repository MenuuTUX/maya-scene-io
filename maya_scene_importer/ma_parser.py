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

import re

# Tokenises a single statement into quoted strings (kept with their quotes) and
# bare words.  Implemented as one regex so the huge numeric arrays in mesh /
# weight blocks tokenise at C speed rather than char-by-char in Python.
_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[^\s"]+')
_UNESCAPE_RE = re.compile(r'\\(.)')


def _unescape(s):
    return _UNESCAPE_RE.sub(r'\1', s)


class MNode:
    """A single Maya dependency / DAG node."""

    __slots__ = ("name", "ntype", "parent", "is_shape", "uuid",
                 "set_attrs", "add_attrs", "_attr_index")

    def __init__(self, name, ntype, parent=None, is_shape=False):
        self.name = name
        self.ntype = ntype
        self.parent = parent          # parent node name (DAG) or None
        self.is_shape = is_shape       # created with the -s flag
        self.uuid = None
        # list of (attr_name, type_or_None, [tokens]) preserving file order
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
            out.extend(toks)
        return out

    def first(self, base, default=None):
        entries = self.attr(base)
        if not entries:
            return default
        return entries[0][2]

    def has_attr(self, base):
        return base in self._index()


class MayaScene:
    def __init__(self):
        self.nodes = {}            # name -> MNode
        self.order = []            # creation order (names)
        self.connections = []      # (src_node, src_attr, dst_node, dst_attr)
        self.by_type = {}          # ntype -> [names]
        # destination attr "node.attr" -> list of source "node.attr"
        self._conn_by_dst = None
        self._conn_by_src = None

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

    def children_of(self, name):
        return [self.nodes[n] for n in self.order
                if self.nodes[n].parent == name]

    def _build_conn_maps(self):
        bydst, bysrc = {}, {}
        for sn, sa, dn, da in self.connections:
            bydst.setdefault("%s.%s" % (dn, da), []).append((sn, sa))
            bysrc.setdefault("%s.%s" % (sn, sa), []).append((dn, da))
        self._conn_by_dst, self._conn_by_src = bydst, bysrc

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
        if self._conn_by_dst is None:
            self._build_conn_maps()
        pref = "%s.%s" % (dst_node, attr_prefix)
        for key, srcs in self._conn_by_dst.items():
            if key.startswith(pref):
                yield key[len(dst_node) + 1:], srcs

    def iter_src_prefix(self, src_node, attr_prefix):
        if self._conn_by_src is None:
            self._build_conn_maps()
        pref = "%s.%s" % (src_node, attr_prefix)
        for key, dsts in self._conn_by_src.items():
            if key.startswith(pref):
                yield key[len(src_node) + 1:], dsts


# ----------------------------------------------------------------------------
# statement splitting
# ----------------------------------------------------------------------------
def _iter_statements(text):
    """Yield raw statement strings (without the trailing ';').

    Comment lines (starting with //) are dropped first.  Statements are split
    on ';' and re-merged whenever a fragment has an odd number of unescaped
    double quotes, which repairs the rare ';' that lives inside a quoted MEL
    string (e.g. inside `script` nodes)."""
    # drop comment lines
    kept = []
    for line in text.split('\n'):
        ls = line.lstrip()
        if ls.startswith('//'):
            continue
        kept.append(line)
    joined = '\n'.join(kept)

    pending = None
    for frag in joined.split(';'):
        if pending is not None:
            frag = pending + ';' + frag
            pending = None
        # unescaped quote parity
        q = frag.count('"') - frag.count('\\"')
        if q % 2:
            pending = frag
            continue
        s = frag.strip()
        if s:
            yield s
    if pending is not None:
        s = pending.strip()
        if s:
            yield s


def _tok(statement):
    """Tokenise into a list of (kind, value): kind is 'str' or 'word'."""
    out = []
    for m in _TOKEN_RE.findall(statement):
        if m[0] == '"':
            out.append(('str', _unescape(m[1:-1])))
        else:
            out.append(('word', m))
    return out


# ----------------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------------
class MAParser:
    def __init__(self):
        self.scene = MayaScene()
        self._cur = None         # current node (target of setAttr/addAttr)
        self._select = None      # node named by an explicit `select`

    def parse_file(self, path):
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            return self.parse_text(fh.read())

    def parse_text(self, text):
        for stmt in _iter_statements(text):
            toks = _tok(stmt)
            if not toks:
                continue
            cmd = toks[0][1] if toks[0][0] == 'word' else None
            handler = self._dispatch.get(cmd)
            if handler:
                handler(self, toks[1:])
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
        node = MNode(name, ntype or 'unknown', parent, is_shape)
        self.scene.add_node(node)
        self._cur = node
        self._select = None

    def _c_rename(self, args):
        # `rename -uid "..."` targets the most recent node
        for i, (kind, val) in enumerate(args):
            if kind == 'word' and val == '-uid' and i + 1 < len(args):
                if self._cur is not None:
                    self._cur.uuid = args[i + 1][1]
                return

    def _target(self):
        return self._select or self._cur

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
        self.scene.connections.append((sn, sa, dn, da))

    def _c_select(self, args):
        # `select -ne "node"` or `select "node"` — sets setAttr target
        name = None
        for kind, val in args:
            if kind == 'str':
                name = val
            elif kind == 'word' and not val.startswith('-'):
                name = val
        if name and name in self.scene.nodes:
            self._select = self.scene.nodes[name]

    def _c_parent(self, args):
        # reparent shapes: `parent -s -nc -r -add "shape" "transform"` etc.
        # Track only the simple `parent "child" "parent"` DAG case so shapes are
        # not orphaned; ignored when flags make it ambiguous.
        strs = [v for (k, v) in args if k == 'str']
        if len(strs) == 2:
            child, newpar = strs
            if child in self.scene.nodes:
                self.scene.nodes[child].parent = newpar

    _dispatch = {
        'createNode': _c_createNode,
        'rename': _c_rename,
        'setAttr': _c_setAttr,
        'addAttr': _c_addAttr,
        'connectAttr': _c_connectAttr,
        'select': _c_select,
        'parent': _c_parent,
    }


def _is_number(tok):
    try:
        float(tok)
        return True
    except ValueError:
        return False


# ----------------------------------------------------------------------------
# value decoding helpers (used by the builder)
# ----------------------------------------------------------------------------
def as_floats(tokens):
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
