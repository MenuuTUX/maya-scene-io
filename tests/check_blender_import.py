"""Run in background Blender with --factory-startup --python-exit-code 1."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from test_native_import import SPARSE_MA
from maya_scene_importer import scene_builder

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 'sparse.ma'
    path.write_text(SPARSE_MA, encoding='utf-8')
    scene_builder.build(str(path), SimpleNamespace(
        import_meshes=True, import_materials=False, import_skeleton=False,
        import_skin=False, import_blendshapes=False))

mesh = bpy.data.objects['triangle'].data
assert len(mesh.polygons) == 1
assert [tuple(v.uv) for v in mesh.uv_layers['detail'].data] == [(0, 0), (1, 0), (0, 1)]
assert [tuple(v.color) for v in mesh.color_attributes['paint'].data] == [
    (0, 0, 1, 1), (1, 0, 0, 1), (0, 1, 0, 1)]
assert mesh.color_attributes.active_color.name == 'paint'
print('BLENDER NATIVE IMPORT CHECK PASSED: sparse UV and color corner values')
