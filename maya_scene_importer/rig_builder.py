# SPDX-License-Identifier: GPL-3.0-or-later
"""
Control-rig reconstruction.

A Maya control rig's *behaviour* (FK/IK blends, set-driven keys, the utility
node graph) has no Blender equivalent and cannot be reproduced 1:1. What *can*
be built automatically, and reliably, is a posable rig:

* the deform skeleton already moves the mesh (skin weights), and the bind
  skeleton is a true FK hierarchy: rotating a bone moves everything below it;
* each posable deform bone gets a clean control shape generated **in the bone's
  own local space**, so it always sits exactly on the bone (no dependency on
  where Maya happened to park its control curves);
* Maya ``ikHandle`` nodes tell us which limbs are IK, so we add real Blender IK
  constraints with small, movable target bones.

The result is an FK + IK rig you can pose immediately, with nothing that can
disturb the bind pose.
"""

import math

import bpy
from mathutils import Vector


def _control_for(bone, curves):
    """A deform joint <name> is driven through a Maya control such as FK<name>."""
    for cand in ("FK" + bone, bone + "_CTRL", bone + "_ctrl", "Ctrl_" + bone,
                 "CTRL_" + bone, bone + "_Ctrl"):
        if cand in curves:
            return True
    return False


def _strip_ik(name):
    for p in ("IKX", "IKx", "IK_", "IK"):
        if name.startswith(p):
            return name[len(p):].lstrip("_")
    return name


def build_rig(data, gm, node_world, arm_obj, controls_col, log):
    stats = dict(controls=0, ik=0)
    arm = arm_obj.data
    deform = [b.name for b in arm.bones if b.use_deform]
    curves = {c.name for c in data.curves()}

    arm.show_bone_custom_shapes = True
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')

    circle = _circle_widget(controls_col)

    # ------------------------------------------------------------------ FK
    for bname in deform:
        if not _control_for(bname, curves):
            continue
        pb = arm_obj.pose.bones.get(bname)
        if pb is None:
            continue
        pb.custom_shape = circle
        pb.use_custom_shape_bone_size = True
        stats['controls'] += 1

    # ------------------------------------------------------------------ IK
    try:
        stats['ik'] = _setup_ik(data, arm_obj, set(deform), controls_col, log)
    except Exception as exc:
        log('WARNING', "IK setup failed: %s" % exc)
        if arm_obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

    log('INFO', "control rig: %d FK control shapes, %d IK limbs"
        % (stats['controls'], stats['ik']))
    return stats


# ---------------------------------------------------------------------------
# widgets (generated in bone-local space, so they always sit on the bone)
# ---------------------------------------------------------------------------
def _hidden(ob, col):
    col.objects.link(ob)
    ob.hide_render = True
    try:
        ob.hide_set(True)
    except Exception:
        pass
    ob.hide_viewport = False        # keep usable as a custom shape; bone draws it
    return ob


def _circle_widget(col):
    name = "WGT_circle"
    if name in bpy.data.objects:
        return bpy.data.objects[name]
    n, r = 20, 0.5
    verts = [(math.cos(2 * math.pi * i / n) * r, 0.0,
              math.sin(2 * math.pi * i / n) * r) for i in range(n)]
    edges = [(i, (i + 1) % n) for i in range(n)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, edges, [])
    me.update()
    return _hidden(bpy.data.objects.new(name, me), col)


def _box_widget(col):
    name = "WGT_box"
    if name in bpy.data.objects:
        return bpy.data.objects[name]
    s = 0.22
    verts = [(-s, -s, -s), (s, -s, -s), (s, s, -s), (-s, s, -s),
             (-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s)]
    edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4),
             (0, 4), (1, 5), (2, 6), (3, 7)]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, edges, [])
    me.update()
    return _hidden(bpy.data.objects.new(name, me), col)


# ---------------------------------------------------------------------------
def _chain_count(arm_obj, start, end):
    pb = arm_obj.pose.bones.get(end)
    n = 0
    while pb is not None:
        n += 1
        if pb.name == start:
            return n
        pb = pb.parent
    return 0


def _setup_ik(data, arm_obj, deform, col, log):
    chains = []
    for ik in data.ik_handles():
        s, e = _strip_ik(ik.start_joint), _strip_ik(ik.end_joint)
        if s in deform and e in deform and s != e and _chain_count(arm_obj, s, e) > 1:
            chains.append((s, e))
    seen = set()
    chains = [(s, e) for (s, e) in chains if not (e in seen or seen.add(e))]
    if not chains:
        return 0

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')
    ebs = arm_obj.data.edit_bones
    targets = {}
    for s, e in chains:
        end_eb = ebs.get(e)
        if end_eb is None:
            continue
        tname = e + "_IK"
        if tname not in ebs:
            tb = ebs.new(tname)
            d = (end_eb.tail - end_eb.head)
            d = d.normalized() if d.length > 1e-6 else Vector((0, 0, 1))
            tb.head = end_eb.tail.copy()
            tb.tail = end_eb.tail + d * (end_eb.length or 0.1)
            tb.use_deform = False
        targets[e] = tname
    bpy.ops.object.mode_set(mode='OBJECT')

    box = _box_widget(col)
    count = 0
    for s, e in chains:
        tname = targets.get(e)
        pb = arm_obj.pose.bones.get(e)
        if pb is None or tname is None:
            continue
        con = pb.constraints.new('IK')
        con.name = "IK"
        con.target = arm_obj
        con.subtarget = tname
        con.chain_count = _chain_count(arm_obj, s, e)
        tpb = arm_obj.pose.bones.get(tname)
        if tpb is not None:
            tpb.custom_shape = box
            tpb.use_custom_shape_bone_size = True
        count += 1
    return count
