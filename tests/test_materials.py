"""Material extraction tests. Plain Python, no Blender: run with

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

HEADER = '//Maya ASCII 2023 scene\nrequires maya "2023";\n'


def surface(ntype, attrs="", name="mtl"):
    """A scene with one shader on a shading engine, plus ``attrs`` lines."""
    return (HEADER
            + 'createNode shadingEngine -n "mtlSG";\n'
            + 'createNode %s -n "%s";\n' % (ntype, name) + attrs
            + 'connectAttr "%s.oc" "mtlSG.ss";\n' % name)


def file_node(name, path):
    return ('createNode file -n "%s";\n\tsetAttr ".ftn" -type "string" "%s";\n'
            % (name, path))


def material(text):
    """(MaterialData for 'mtlSG', model) from Maya ASCII ``text``."""
    fd, path = tempfile.mkstemp(suffix=".ma")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        scene = ma_parser.parse_file(path)
    finally:
        os.remove(path)
    model = ma_model.extract(scene)
    return model.materials()["mtlSG"], model


class PbrValues(unittest.TestCase):
    def test_arnold_long_names(self):
        md, _ = material(surface("aiStandardSurface", (
            '\tsetAttr ".baseColor" -type "float3" 0.2 0.4 0.6;\n'
            '\tsetAttr ".metalness" 0.8;\n'
            '\tsetAttr ".specularRoughness" 0.35;\n'
            '\tsetAttr ".opacity" -type "float3" 0.5 0.5 0.5;\n')))
        self.assertEqual(md.color, (0.2, 0.4, 0.6))
        self.assertAlmostEqual(md.metalness, 0.8)
        self.assertAlmostEqual(md.roughness, 0.35)
        self.assertAlmostEqual(md.transparency[0], 0.5)

    def test_autodesk_standard_surface_long_names(self):
        md, _ = material(surface("standardSurface", (
            '\tsetAttr ".metalness" 1;\n'
            '\tsetAttr ".specularRoughness" 0.1;\n')))
        self.assertAlmostEqual(md.metalness, 1.0)
        self.assertAlmostEqual(md.roughness, 0.1)

    def test_short_names(self):
        md, _ = material(surface("aiStandardSurface", (
            '\tsetAttr ".m" 0.7;\n\tsetAttr ".sr" 0.6;\n')))
        self.assertAlmostEqual(md.metalness, 0.7)
        self.assertAlmostEqual(md.roughness, 0.6)

    def test_unset_roughness_is_the_shaders_default(self):
        md, _ = material(surface("aiStandardSurface"))
        self.assertAlmostEqual(md.roughness, 0.2)
        self.assertEqual(md.metalness, 0.0)

    def test_emission_weight_scales_colour(self):
        md, _ = material(surface("aiStandardSurface", (
            '\tsetAttr ".emission" 0.5;\n'
            '\tsetAttr ".emissionColor" -type "float3" 1 0.5 0;\n')))
        self.assertEqual(md.incandescence, (1.0, 0.5, 0.0))
        self.assertAlmostEqual(md.emission_strength, 0.5)

    def test_emission_weight_alone_glows_white(self):
        md, _ = material(surface("aiStandardSurface",
                                 '\tsetAttr ".emission" 1;\n'))
        self.assertEqual(md.incandescence, (1.0, 1.0, 1.0))

    def test_emission_colour_without_weight_is_off(self):
        md, _ = material(surface("aiStandardSurface", (
            '\tsetAttr ".emissionColor" -type "float3" 1 0 0;\n')))
        self.assertEqual(md.emission_strength, 0.0)

    def test_nothing_authored_keeps_name_matched_maps_at_full_strength(self):
        md, _ = material(surface("aiStandardSurface"))
        self.assertEqual(md.emission_strength, 1.0)
        self.assertEqual(md.incandescence, (0.0, 0.0, 0.0))

    def test_lambert_is_untouched(self):
        md, _ = material(surface("lambert", (
            '\tsetAttr ".c" -type "float3" 0.1 0.2 0.3;\n'
            '\tsetAttr ".m" 0.9;\n\tsetAttr ".sr" 0.9;\n'), name="mtl"))
        self.assertEqual(md.color, (0.1, 0.2, 0.3))
        self.assertIsNone(md.metalness)
        self.assertIsNone(md.roughness)


class PbrTextures(unittest.TestCase):
    def test_file_nodes_on_metalness_and_roughness(self):
        md, _ = material(surface("aiStandardSurface", (
            file_node("fM", "sourceimages/wood_met.png")
            + file_node("fR", "sourceimages/wood_rgh.png")))
            + 'connectAttr "fM.oc" "mtl.metalness";\n'
            + 'connectAttr "fR.oc" "mtl.specularRoughness";\n')
        self.assertEqual(md.maya_textures["metallic"],
                         "sourceimages/wood_met.png")
        self.assertEqual(md.maya_textures["roughness"],
                         "sourceimages/wood_rgh.png")

    def test_file_node_on_short_names(self):
        md, _ = material(surface("aiStandardSurface", (
            file_node("fR", "r.png")))
            + 'connectAttr "fR.oc" "mtl.sr";\n')
        self.assertEqual(md.maya_textures["roughness"], "r.png")

    def test_ai_normal_map(self):
        md, _ = material(surface("aiStandardSurface", (
            file_node("fN", "n.png")
            + 'createNode aiNormalMap -n "nm";\n'))
            + 'connectAttr "fN.oc" "nm.input";\n'
            + 'connectAttr "nm.outValue" "mtl.normalCamera";\n')
        self.assertEqual(md.maya_textures["normal"], "n.png")
        self.assertEqual(md.normal_mode, 1)

    def test_bump2d_still_works(self):
        md, _ = material(surface("lambert", (
            file_node("fN", "b.png")
            + 'createNode bump2d -n "b2";\n\tsetAttr ".bi" 0;\n'
            '\tsetAttr ".bd" 0.5;\n'))
            + 'connectAttr "fN.oa" "b2.bv";\n'
            + 'connectAttr "b2.o" "mtl.n";\n')
        self.assertEqual(md.maya_textures["normal"], "b.png")
        self.assertEqual(md.normal_mode, 0)
        self.assertAlmostEqual(md.bump_depth, 0.5)

    def test_base_colour_texture_still_works(self):
        md, _ = material(surface("aiStandardSurface", (
            file_node("fC", "sourceimages/Body_BaseColor.png")))
            + 'connectAttr "fC.oc" "mtl.baseColor";\n')
        self.assertEqual(md.maya_textures["baseColor"],
                         "sourceimages/Body_BaseColor.png")
        self.assertEqual(md.tex_prefix, "Body")


class UnreadInputs(unittest.TestCase):
    def test_unrecognised_texture_input_is_reported(self):
        _, model = material(surface("aiStandardSurface", (
            file_node("fX", "x.png")))
            + 'connectAttr "fX.oc" "mtl.specularColor";\n')
        self.assertTrue(any("specularColor" in w or "'sc'" in w
                            for w in model.warnings), model.warnings)

    def test_recognised_inputs_do_not_warn(self):
        _, model = material(surface("aiStandardSurface", (
            file_node("fR", "r.png")))
            + 'connectAttr "fR.oc" "mtl.specularRoughness";\n')
        self.assertEqual(model.warnings, [])


if __name__ == "__main__":
    unittest.main()
