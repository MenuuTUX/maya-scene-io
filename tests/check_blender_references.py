"""Imports a scene that references another file, in Blender, and checks it.

Needs Blender, so unittest discovery skips it. Run:

    blender -b --factory-startup --python tests/check_blender_references.py
"""
import os
import struct
import sys
import tempfile
import zlib

import bpy

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import maya_scene_importer                                  # noqa: E402
from maya_scene_importer import rig_builder                 # noqa: E402

RIG = '''//Maya ASCII 2023 scene
requires maya "2023";
createNode transform -n "pl";
createNode mesh -n "plShape" -p "pl";
	setAttr -k off ".v";
	setAttr -s 4 ".vt[0:3]" -0.5 0 0.5 0.5 0 0.5 -0.5 0 -0.5 0.5 0 -0.5;
	setAttr -s 4 ".ed[0:3]" 0 1 0 1 3 0 3 2 0 2 0 0;
	setAttr -s 1 ".fc[0]" -type "polyFaces" f 4 0 1 2 3 mu 0 4 0 1 2 3;
	setAttr -s 4 ".uvst[0].uvsp[0:3]" -type "float2" 0 0 1 0 1 1 0 1;
createNode shadingEngine -n "mtlSG";
createNode lambert -n "mtl";
createNode file -n "tex";
	setAttr ".ftn" -type "string" "sourceimages/skin.png";
connectAttr "tex.oc" "mtl.c";
connectAttr "mtl.oc" "mtlSG.ss";
connectAttr "plShape.iog" "mtlSG.dsm" -na;
'''

# the scene lives in shots/ and the rig in ../assets, as in a project
SHOT = '''//Maya ASCII 2023 scene
requires maya "2023";
file -rdi 1 -ns "rig" -rfn "rigRN" -typ "mayaAscii" "../assets/rig.ma";
file -r -ns "rig" -dr 1 -rfn "rigRN" -typ "mayaAscii" "../assets/rig.ma";
'''


def png(path):
    raw = b"\x00" + b"\x80\x80\x80" * 4
    chunk = lambda tag, data: (struct.pack(">I", len(data)) + tag + data
                               + struct.pack(">I", zlib.crc32(tag + data)))
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n"
                 + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw * 2))
                 + chunk(b"IEND", b""))


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    return ok


folder = tempfile.mkdtemp(prefix="mayaio_test_")
os.makedirs(os.path.join(folder, "assets", "sourceimages"))
os.makedirs(os.path.join(folder, "shots"))
png(os.path.join(folder, "assets", "sourceimages", "skin.png"))
with open(os.path.join(folder, "assets", "rig.ma"), "w") as fh:
    fh.write(RIG)
shot = os.path.join(folder, "shots", "main.ma")
with open(shot, "w") as fh:
    fh.write(SHOT)

maya_scene_importer.register()
bpy.ops.import_scene.maya_ascii(filepath=shot)

mat = bpy.data.materials.get("rig:mtl")
images = [n.image.filepath for n in mat.node_tree.nodes
          if n.type == 'TEX_IMAGE' and n.image] if mat else []
results = [
    check("referenced mesh is imported with its namespace",
          "rig:pl" in bpy.data.objects),
    check("texture is found beside the referenced file, not the scene",
          any(p.endswith("skin.png") and "assets" in p for p in images)),
    check("FK control found through a namespace",
          rig_builder._control_for("ns:Hip_R", {"ns:FKHip_R"})),
    check("FK control still found without one",
          rig_builder._control_for("Hip_R", {"FKHip_R"})),
    check("IK prefix stripped after the namespace",
          rig_builder._strip_ik("ns:IKXHip_R") == "ns:Hip_R"),
    check("IK prefix stripped without a namespace",
          rig_builder._strip_ik("IKXHip_R") == "Hip_R"),
]
sys.exit(0 if all(results) else 1)
