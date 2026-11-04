"""Referenced-file loading tests. Plain Python, no Blender: run with

    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "maya_scene_importer"))
import ma_model                                    # noqa: E402
import ma_parser                                   # noqa: E402
import ma_refs                                     # noqa: E402

HEADER = '//Maya ASCII 2023 scene\nrequires maya "2023";\n'

# a plane with a textured shader, plus Maya's shared default camera
RIG = HEADER + '''createNode transform -s -n "persp";
createNode camera -s -n "perspShape" -p "persp";
createNode transform -n "grp";
createNode transform -n "pl" -p "grp";
createNode mesh -n "plShape" -p "pl";
	setAttr -s 4 ".vt[0:3]" -0.5 0 0.5 0.5 0 0.5 -0.5 0 -0.5 0.5 0 -0.5;
createNode shadingEngine -n "mtlSG";
createNode lambert -n "mtl";
createNode file -n "tex";
	setAttr ".ftn" -type "string" "sourceimages/skin.png";
connectAttr "tex.oc" "mtl.c";
connectAttr "mtl.oc" "mtlSG.ss";
connectAttr "plShape.iog" "mtlSG.dsm" -na;
'''


def ref_lines(path, ns, rfn=None, depth=1):
    """The lines Maya writes for one reference. A reference nested deeper than
    the scene's own appears in the scene only as its `-rdi` depth line."""
    rfn = rfn or ns + "RN"
    text = ('file -rdi %d -ns "%s" -rfn "%s" -typ "mayaAscii" "%s";\n'
            % (depth, ns, rfn, path))
    if depth == 1:
        text += ('file -r -ns "%s" -dr 1 -rfn "%s" -typ "mayaAscii" "%s";\n'
                 % (ns, rfn, path))
    return text


class Workspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text, sub=""):
        folder = os.path.join(self.dir, sub)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def load(self, path):
        scene = ma_parser.parse_file(path)
        ma_refs.load_references(scene, path)
        return scene


class Loading(Workspace):
    def test_nodes_get_the_namespace(self):
        self.write("rig.ma", RIG)
        main = self.write("main.ma", HEADER + ref_lines("rig.ma", "rig"))
        scene = self.load(main)
        self.assertIn("rig:pl", scene.nodes)
        self.assertIn("rig:plShape", scene.nodes)
        self.assertNotIn("pl", scene.nodes)

    def test_hierarchy_and_connections_follow(self):
        self.write("rig.ma", RIG)
        main = self.write("main.ma", HEADER + ref_lines("rig.ma", "rig"))
        scene = self.load(main)
        self.assertEqual(scene.nodes["rig:pl"].parent, "rig:grp")
        model = ma_model.extract(scene)
        self.assertIn("rig:mtlSG", model.materials())
        self.assertIn("rig:plShape", model.mesh_material_map())

    def test_each_reference_counts_once(self):
        self.write("rig.ma", RIG)
        main = self.write("main.ma", HEADER + ref_lines("rig.ma", "rig"))
        scene = ma_parser.parse_file(main)
        self.assertEqual(scene.references, ["rig.ma"])

    def test_the_same_file_twice(self):
        self.write("rig.ma", RIG)
        main = self.write("main.ma", HEADER + ref_lines("rig.ma", "a")
                          + ref_lines("rig.ma", "b"))
        scene = self.load(main)
        for ns in ("a", "b"):
            self.assertIn("%s:plShape" % ns, scene.nodes)

    def test_nested_references_compose_namespaces(self):
        self.write("inner.ma", RIG)
        self.write("outer.ma", HEADER + ref_lines("inner.ma", "inner"))
        main = self.write("main.ma", HEADER + ref_lines("outer.ma", "outer")
                          + ref_lines("inner.ma", "inner", depth=2))
        scene = self.load(main)
        self.assertIn("outer:inner:plShape", scene.nodes)
        self.assertEqual(sum(1 for n in scene.nodes if n.endswith("plShape")),
                         1)

    def test_shared_default_nodes_are_not_duplicated(self):
        self.write("rig.ma", RIG)
        main = self.write("main.ma", HEADER
                          + 'createNode transform -s -n "persp";\n'
                          + ref_lines("rig.ma", "rig"))
        scene = self.load(main)
        self.assertNotIn("rig:persp", scene.nodes)
        self.assertEqual([n for n in scene.nodes if n == "persp"], ["persp"])

    def test_main_scene_can_connect_to_referenced_nodes(self):
        self.write("rig.ma", RIG)
        main = self.write("main.ma", HEADER + ref_lines("rig.ma", "rig")
                          + 'createNode lambert -n "newMtl";\n'
                          + 'connectAttr "newMtl.oc" "rig:mtlSG.ss";\n')
        scene = self.load(main)
        self.assertIn(("newMtl", "oc", "rig:mtlSG", "ss"), scene.connections)

    def test_textures_resolve_from_the_referenced_files_folder(self):
        self.write("rig.ma", RIG, sub="assets")
        main = self.write("main.ma", HEADER + ref_lines("assets/rig.ma", "rig"))
        scene = self.load(main)
        md = ma_model.extract(scene).materials()["rig:mtlSG"]
        self.assertEqual(md.basedir, os.path.join(self.dir, "assets"))

    def test_texture_name_prefix_ignores_the_namespace(self):
        self.write("rig.ma", HEADER + 'createNode shadingEngine -n "BodySG";\n'
                   'createNode lambert -n "Body_MAT";\n'
                   'connectAttr "Body_MAT.oc" "BodySG.ss";\n')
        main = self.write("main.ma", HEADER + ref_lines("rig.ma", "rig"))
        md = ma_model.extract(self.load(main)).materials()["rig:BodySG"]
        self.assertEqual(md.tex_prefix, "Body")

    def test_main_scene_materials_have_no_basedir(self):
        main = self.write("main.ma", surface_scene())
        md = ma_model.extract(ma_parser.parse_file(main)).materials()["mtlSG"]
        self.assertIsNone(md.basedir)


def surface_scene():
    return (HEADER + 'createNode shadingEngine -n "mtlSG";\n'
            'createNode lambert -n "mtl";\nconnectAttr "mtl.oc" "mtlSG.ss";\n')


class Finding(Workspace):
    def test_relative_path_in_a_project_folder(self):
        self.write("rig.ma", RIG, sub="scenes")
        main = self.write("main.ma", HEADER + ref_lines("scenes/rig.ma", "rig"),
                          sub="scenes")
        # path is project relative: one folder up from the scene
        scene = self.load(main)
        self.assertIn("rig:plShape", scene.nodes)

    def test_absolute_path_from_another_machine(self):
        self.write("rig.ma", RIG)
        main = self.write("main.ma", HEADER + ref_lines(
            "//server/Work Area/char/rig.ma", "rig"))
        scene = self.load(main)
        self.assertIn("rig:plShape", scene.nodes)

    def test_file_name_case_is_ignored(self):
        self.write("Rig.ma", RIG)
        main = self.write("main.ma", HEADER + ref_lines("rig.MA", "rig"))
        scene = self.load(main)
        self.assertIn("rig:plShape", scene.nodes)


class Failing(Workspace):
    def test_missing_file_is_reported_not_fatal(self):
        main = self.write("main.ma", HEADER + 'createNode transform -n "keep";\n'
                          + ref_lines("gone.ma", "rig"))
        scene = self.load(main)
        self.assertIn("keep", scene.nodes)
        self.assertTrue(any("gone.ma" in n for n in scene.notes), scene.notes)

    def test_reference_cycle_stops(self):
        self.write("a.ma", HEADER + 'createNode transform -n "ta";\n'
                   + ref_lines("b.ma", "b"))
        self.write("b.ma", HEADER + 'createNode transform -n "tb";\n'
                   + ref_lines("a.ma", "a"))
        main = self.write("main.ma", HEADER + ref_lines("a.ma", "a"))
        scene = self.load(main)
        self.assertIn("a:ta", scene.nodes)
        self.assertTrue(any("loop" in n for n in scene.notes), scene.notes)

    def test_damaged_referenced_file_is_reported(self):
        self.write("bad.ma", "\x00\x01 this is not a maya scene")
        main = self.write("main.ma", HEADER + ref_lines("bad.ma", "rig"))
        scene = self.load(main)
        self.assertTrue(any("bad.ma" in n for n in scene.notes), scene.notes)


class Edits(Workspace):
    def edits(self, body):
        self.write("rig.ma", RIG)
        main = self.write("main.ma", HEADER + ref_lines("rig.ma", "rig")
                          + 'createNode reference -n "rigRN";\n'
                          + '\tsetAttr ".ed" -type "dataReferenceEdits"\n'
                          + body + ';\n')
        return self.load(main)

    def test_edits_are_reported(self):
        scene = self.edits('\t\t"rigRN"\n\t\t"rigRN" 0\n'
                           '\t\t5 "rig:grp.translateX" " -av 5"')
        self.assertTrue(any("edits" in n for n in scene.notes), scene.notes)

    def test_an_empty_edit_list_is_not(self):
        scene = self.edits('\t\t"rigRN"\n\t\t"rigRN" 0')
        self.assertFalse(any("edits" in n for n in scene.notes), scene.notes)


if __name__ == "__main__":
    unittest.main()
