"""Builds a synthetic Arnold-shaded scene in Blender and checks the result.

Needs Blender, so unittest discovery skips it. Run:

    blender -b --factory-startup --python tests/check_blender_materials.py
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

SCENE = '''//Maya ASCII 2023 scene
requires maya "2023";
createNode transform -n "pl";
createNode mesh -n "plShape" -p "pl";
	setAttr -k off ".v";
	setAttr -s 4 ".vt[0:3]" -0.5 0 0.5 0.5 0 0.5 -0.5 0 -0.5 0.5 0 -0.5;
	setAttr -s 4 ".ed[0:3]" 0 1 0 1 3 0 3 2 0 2 0 0;
	setAttr -s 1 ".fc[0]" -type "polyFaces" f 4 0 1 2 3 mu 0 4 0 1 2 3;
	setAttr -s 4 ".uvst[0].uvsp[0:3]" -type "float2" 0 0 1 0 1 1 0 1;
createNode transform -n "pl2";
createNode mesh -n "pl2Shape" -p "pl2";
	setAttr -k off ".v";
	setAttr -s 4 ".vt[0:3]" -0.5 0 0.5 0.5 0 0.5 -0.5 0 -0.5 0.5 0 -0.5;
	setAttr -s 4 ".ed[0:3]" 0 1 0 1 3 0 3 2 0 2 0 0;
	setAttr -s 1 ".fc[0]" -type "polyFaces" f 4 0 1 2 3 mu 0 4 0 1 2 3;
	setAttr -s 4 ".uvst[0].uvsp[0:3]" -type "float2" 0 0 1 0 1 1 0 1;
createNode shadingEngine -n "glowSG";
createNode aiStandardSurface -n "glow";
connectAttr "glow.oc" "glowSG.ss";
connectAttr "pl2Shape.iog" "glowSG.dsm" -na;
createNode shadingEngine -n "steelSG";
createNode aiStandardSurface -n "steel";
	setAttr ".metalness" 0.9;
	setAttr ".emission" 0.5;
	setAttr ".emissionColor" -type "float3" 1 0.5 0;
	setAttr ".opacity" -type "float3" 0.5 0.5 0.5;
createNode file -n "fR";
	setAttr ".ftn" -type "string" "sourceimages/steel_gloss.png";
connectAttr "fR.oc" "steel.specularRoughness";
connectAttr "steel.oc" "steelSG.ss";
connectAttr "plShape.iog" "steelSG.dsm" -na;
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
os.makedirs(os.path.join(folder, "sourceimages"))
png(os.path.join(folder, "sourceimages", "steel_gloss.png"))
# found by name next to the scene; the shader itself says nothing about emission
png(os.path.join(folder, "glow_Emissive.png"))
scene = os.path.join(folder, "steel.ma")
with open(scene, "w") as fh:
    fh.write(SCENE)

maya_scene_importer.register()
bpy.ops.import_scene.maya_ascii(filepath=scene)
mat = bpy.data.materials["steel"]
bsdf = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
rough = bsdf.inputs["Roughness"].links
glow = next(n for n in bpy.data.materials["glow"].node_tree.nodes
            if n.type == 'BSDF_PRINCIPLED')
results = [
    check("metalness value 0.9",
          abs(bsdf.inputs["Metallic"].default_value - 0.9) < 1e-4),
    check("roughness fed by the Maya file node",
          bool(rough) and rough[0].from_node.type == 'TEX_IMAGE'
          and "steel_gloss" in rough[0].from_node.image.filepath),
    check("emission strength 0.5",
          abs(bsdf.inputs["Emission Strength"].default_value - 0.5) < 1e-4),
    check("emission colour orange",
          tuple(round(c, 2) for c in bsdf.inputs["Emission Color"]
                .default_value[:3]) == (1.0, 0.5, 0.0)),
    check("opacity 0.5 becomes alpha",
          abs(bsdf.inputs["Alpha"].default_value - 0.5) < 1e-4),
    check("emission map found by name still glows at full strength",
          bool(glow.inputs["Emission Color"].links)
          and abs(glow.inputs["Emission Strength"].default_value - 1.0) < 1e-4),
]
sys.exit(0 if all(results) else 1)
