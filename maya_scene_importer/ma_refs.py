# SPDX-License-Identifier: GPL-3.0-or-later
"""
Load the files a Maya scene references.

A production scene often holds little itself: the rig and props live in other
files, pulled in with ``file -r -ns "ns" "path"``. The parser records those
lines; this module reads each referenced file and merges its nodes into the
scene, the way Maya does. Every node gets the reference's namespace
(``ns:name``), so two references to one file stay apart and connections written
in the main scene (``connectAttr "ns:ctrl.o" ...``) find their nodes.

Maya's shared default nodes (the ``persp`` camera and so on, created with
``-s``) are not namespaced and are not added twice.

Not applied: the edits a scene stores for a reference, such as moving the rig
into place. Their format is undocumented, so the loader only says that edits
exist. Pure Python, no ``bpy``.
"""

import os

try:
    from . import ma_parser, mb_parser
except ImportError:                 # plain-Python use (tests, diagnostics)
    import ma_parser
    import mb_parser

MAX_DEPTH = 8                       # references inside references inside ...


def _parse(path):
    if mb_parser.is_binary(path):
        return mb_parser.parse_file(path)
    return ma_parser.parse_file(path)


def _find_file(folder, name):
    """``folder/name`` if it exists, matching the name's case as Windows does."""
    full = os.path.join(folder, name)
    if os.path.isfile(full):
        return full
    try:
        lowered = {f.lower(): f for f in os.listdir(folder)}
    except OSError:
        return None
    hit = lowered.get(name.lower())
    return os.path.join(folder, hit) if hit else None


def resolve_path(path, scene_dir):
    """Where a referenced file is on this computer, or None.

    Tried in turn: the path as written (same machine), then ever shorter tails
    of it under the scene's folder and the folders above it, which covers
    project-relative paths and files copied from a server or another drive."""
    path = os.path.expandvars(path)
    if os.path.isfile(path):
        return path
    parts = [p for p in path.replace('\\', '/').split('/')
             if p and not p.endswith(':')]
    roots, folder = [], os.path.abspath(scene_dir)
    for _ in range(3):
        roots.append(folder)
        folder = os.path.dirname(folder)
    for root in dict.fromkeys(roots):
        for k in range(len(parts)):
            folder = os.path.join(root, *parts[k:-1])
            hit = _find_file(folder, parts[-1]) if os.path.isdir(folder) \
                else None
            if hit:
                return hit
    return None


def _has_edits(scene, info):
    """True when the reference node stores edits (moves, overrides ...).

    The ``.ed`` list names the reference node and counters, then one entry per
    edit; anything else in it means an edit was made."""
    node = scene.nodes.get(info.get('node') or '')
    if node is None:
        return False
    for tok in node.attr_tokens('ed'):
        tok = str(tok)
        if tok == node.name or tok.lstrip('-').isdigit():
            continue
        return True
    return False


def _merge(scene, sub, prefix, origin):
    """Move the nodes and connections of ``sub`` into ``scene``."""
    shared = {n for n, node in sub.nodes.items() if node.is_shape}

    def rename(name):
        if name is None:
            return None
        if name.startswith(':'):                    # root namespace
            return name[1:]
        if name in sub.nodes and name not in shared:
            return prefix + name
        return name

    for name in sub.order:
        node = sub.nodes[name]
        if name in shared:
            if name in scene.nodes:
                continue
            node.parent = rename(node.parent)
        else:
            node.name = prefix + name
            node.parent = rename(node.parent)
        node.origin = origin
        scene.add_node(node)
    for sn, sa, dn, da in sub.connections:
        scene.connections.append((rename(sn), sa, rename(dn), da))
    for path, name in sub.paths.items():
        full = '|' + '|'.join(rename(seg) for seg in path.split('|')[1:])
        scene.paths[full] = rename(name)
        scene._path_of[rename(name)] = full
    scene._conn_by_dst = scene._conn_by_src = scene._dst_by_node = None


def load_references(scene, filepath, _stack=None):
    """Merge every file ``scene`` references into it. Problems are added to
    ``scene.notes`` and never raise: a missing or damaged reference leaves the
    rest of the scene intact."""
    stack = _stack or (os.path.abspath(filepath),)
    scene_dir = os.path.dirname(os.path.abspath(filepath))
    for info in scene.reference_info:
        label = info['namespace'] or os.path.basename(info['path'])
        found = resolve_path(info['path'], scene_dir)
        if not found:
            scene.notes.append("reference '%s' was not found (%s); copy the "
                               "file next to the scene" % (label, info['path']))
            continue
        found = os.path.abspath(found)
        if found in stack:
            scene.notes.append("reference '%s' loops back to %s; skipped"
                               % (label, os.path.basename(found)))
            continue
        if len(stack) > MAX_DEPTH:
            scene.notes.append("reference '%s' is nested more than %d deep; "
                               "skipped" % (label, MAX_DEPTH))
            continue
        try:
            sub = _parse(found)
        except Exception as exc:        # noqa: BLE001 - never abort the import
            scene.notes.append("reference '%s' could not be read (%s): %s"
                               % (label, os.path.basename(found), exc))
            continue
        load_references(sub, found, stack + (found,))
        scene.notes.extend(sub.notes)
        if not sub.nodes:
            scene.notes.append("reference '%s' (%s) holds no Maya nodes"
                               % (label, os.path.basename(found)))
            continue
        ns = info['namespace']
        _merge(scene, sub, ns.strip(':') + ':' if ns else '',
               os.path.dirname(found))
        if sub.linear_unit != scene.linear_unit:
            scene.notes.append("reference '%s' uses %s but the scene uses %s; "
                               "its sizes are not converted"
                               % (label, sub.linear_unit, scene.linear_unit))
        if _has_edits(scene, info):
            scene.notes.append("reference '%s' has edits made in this scene, "
                               "such as moving it; they are not applied" % label)
