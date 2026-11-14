"""Run with python -m unittest discover -s tests (no Blender required)."""
import io
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'maya_scene_importer'))
import ma_parser
import ma_model
import mb_parser


SPARSE_MA = '''createNode transform -n "triangle";
createNode mesh -n "triangleShape" -p "triangle";
setAttr ".vt[0:2]" -type "float3" 0 0 0 1 0 0 0 1 0;
setAttr ".ed[0:2]" 0 1 0 1 2 1 2 0 1;
setAttr ".fc[0]" -type "polyFaces" f 3 0 1 2 mu 7 3 0 1 2 mc 4 3 2 0 1;
setAttr ".uvst[7].uvsn" -type "string" "detail";
setAttr ".uvst[7].uvsp[0:2]" -type "float2" 0 0 1 0 0 1;
setAttr ".clst[4].clsn" -type "string" "paint";
setAttr ".clst[4].clsp[0:2]" -type "float4" 1 0 0 1 0 1 0 1 0 0 1 1;
setAttr ".ccls" -type "string" "paint";
'''


def chunk(tag, payload):
    return tag + struct.pack('>I', len(payload)) + payload + b'\0' * (-len(payload) % 4)


def group(kind, *children):
    return chunk(b'FOR4', kind + b''.join(children))


def binary_scene(*attrs):
    return group(b'Maya', group(b'XFRM', chunk(b'CREA', b'\0triangle\0')),
                 group(b'DMSH', chunk(b'CREA', b'\0triangleShape\0triangle\0'), *attrs))


class NativeImportTests(unittest.TestCase):
    def test_sparse_sets(self):
        scene = ma_parser.MAParser()._parse_lines(io.StringIO(SPARSE_MA))
        mesh, = ma_model.MAImport(scene).meshes()
        self.assertEqual(mesh.uv_sets, [('detail', [(0, 0), (1, 0), (0, 1)])])
        self.assertEqual(mesh.face_uv_sets, [[[0, 1, 2]]])
        self.assertEqual(mesh.color_sets[0][0], 'paint')
        self.assertEqual(mesh.color_sets[0][2], [[2, 0, 1]])

    def test_statement_after_escaped_backslash(self):
        text = r'''createNode file -n "texture";
setAttr ".ftn" -type "string" "C:\\textures\\";
setAttr ".label" -type "string" "D:\\more\\";
createNode transform -n "after";
'''
        scene = ma_parser.MAParser()._parse_lines(io.StringIO(text))
        self.assertIn('after', scene.nodes)
        self.assertEqual(scene.nodes['texture'].attr_tokens('ftn'), ['C:\\textures\\'])
        self.assertEqual(scene.nodes['texture'].attr_tokens('label'), ['D:\\more\\'])

    def test_quotes_and_semicolon_in_string(self):
        text = r'''createNode script -n "script";
setAttr ".b" -type "string" "say \"hello; world\"";
createNode transform -n "after";
'''
        scene = ma_parser.MAParser()._parse_lines(io.StringIO(text))
        self.assertEqual(scene.nodes['script'].attr_tokens('b'), ['say "hello; world"'])
        self.assertIn('after', scene.nodes)

    def test_binary_damaged_attribute_keeps_later_values(self):
        data = binary_scene(chunk(b'DBL#', b'bad\0\0' + struct.pack('>I', 10)),
                            chunk(b'FLT3', b'vt[0]\0\0' + struct.pack('>3f', 1, 2, 3)))
        scene = mb_parser.MBReader(data).read()
        self.assertEqual(scene.skipped, 1)
        self.assertEqual(scene.nodes['triangleShape'].attr_tokens('vt'), ['1', '2', '3'])
        self.assertIn("damaged attribute 'bad'", scene.notes[0])

    def test_binary_packed_mesh(self):
        payload = (struct.pack('>I9f', 9, 0, 0, 0, 1, 0, 0, 0, 1, 0)
                   + struct.pack('>I6I', 6, 0x80000000, 1, 1, 2, 2, 0)
                   + struct.pack('>I3I', 3, 0, 1, 0x60000002)
                   + struct.pack('>III', 0, 1, 0) + b'map1\0'
                   + struct.pack('>I6f', 6, 0, 0, 1, 0, 0, 1)
                   + struct.pack('>I3i', 3, 0, 1, 2))
        scene = mb_parser.MBReader(binary_scene(chunk(b'MESH', b'o\0\0' + payload))).read()
        mesh, = ma_model.MAImport(scene).meshes()
        self.assertEqual(mesh.faces, [[0, 1, 2]])
        self.assertEqual(mesh.edges[0], (0, 1, True))
        self.assertEqual(mesh.face_uv_sets, [[[0, 1, 2]]])
        self.assertEqual(scene.skipped, 0)


if __name__ == '__main__':
    unittest.main()
