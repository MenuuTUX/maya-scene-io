# SPDX-License-Identifier: GPL-3.0-or-later
"""
Operators, sidebar panels and registration shared by every edition of the
add-on.  This module imports Maya ASCII (.ma) and Maya Binary (.mb) and exports
Maya ASCII; an edition that supports more subclasses these classes and passes
its own list to :func:`register`.
"""

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       PointerProperty, StringProperty)
from bpy_extras.io_utils import ImportHelper, ExportHelper

from . import mb_parser

_HIDDEN_ITEMS = [
    ('HIDE', "Import as Hidden",
     "Import them hidden, in a separate collection (most faithful)"),
    ('SKIP_HELPERS', "Skip Rig Helpers",
     "Skip throwaway rig-construction meshes; keep real geometry hidden"),
    ('SHOW', "Import Visible", "Import everything fully visible"),
]


class IMPORT_SCENE_OT_maya_ascii(bpy.types.Operator, ImportHelper):
    """Import a Maya scene (.ma or .mb): meshes, skeleton, skin, shapes, materials"""
    bl_idname = "import_scene.maya_ascii"
    bl_label = "Import Maya Scene (.ma/.mb)"
    bl_options = {'REGISTER', 'UNDO', 'PRESET'}

    filename_ext = ".ma"
    filter_glob: StringProperty(default="*.ma;*.mb", options={'HIDDEN'})
    failure_hint = "This add-on reads standard Maya .ma and .mb files."

    def scene_parser(self):
        """The function that reads ``self.filepath``; None means Maya ASCII.
        Decided by content, so a binary scene saved with a .ma name reads too."""
        if mb_parser.is_binary(self.filepath):
            return mb_parser.parse_file
        return None

    # -- orientation ------------------------------------------------------
    global_scale: FloatProperty(
        name="Scale", default=0.01, min=1e-6, max=1e6,
        soft_min=0.001, soft_max=10.0,
        description="Uniform import scale. Maya works in centimetres, so 0.01 "
                    "gives a real-world-sized result in metres; use 1.0 for raw "
                    "Maya units")
    apply_axis: BoolProperty(
        name="Y-up to Z-up", default=True,
        description="Convert Maya's Y-up axis to Blender's Z-up")

    # -- what to import ---------------------------------------------------
    import_meshes: BoolProperty(name="Meshes", default=True,
                                description="Polygon meshes, UVs and edges")
    import_normals: BoolProperty(     # kept so saved presets still load
        name="Custom Split Normals", default=False, options={'HIDDEN'},
        description="Not supported: shading comes from Maya's edge hardness")
    import_materials: BoolProperty(
        name="Materials + Textures", default=True,
        description="Rebuild materials as Principled BSDF and auto-wire the PBR "
                    "texture set found next to the .ma file")
    import_blendshapes: BoolProperty(
        name="Blend Shapes", default=True,
        description="Import blendShape targets as shape keys")
    import_cameras: BoolProperty(name="Cameras", default=True,
                                 description="Import non-default cameras")
    import_lights: BoolProperty(name="Lights", default=True,
                                description="Import lights")
    import_animation: BoolProperty(
        name="Animation", default=True,
        description="Import time-based animation curves as keyframes")

    import_skeleton: BoolProperty(
        name="Skeleton (Armature)", default=True,
        description="Import the joint hierarchy at its bind pose")
    import_skin: BoolProperty(
        name="Skin Weights", default=True,
        description="Import skinCluster weights and bind meshes to the armature")

    orient_bones: BoolProperty(
        name="Orient Bones to Children", default=True,
        description="Point each bone at its child, the way Blender draws and "
                    "poses a skeleton. Off keeps Maya's joint axes exactly; "
                    "either way the mesh deforms identically")

    hidden_handling: EnumProperty(
        name="Hidden Objects", default='HIDE',
        description="What to do with objects Maya had hidden (rig-fit helpers, "
                    "closed-mouth teeth/tongue, etc.)",
        items=_HIDDEN_ITEMS)

    # -- control rig ------------------------------------------------------
    experimental_rig: BoolProperty(
        name="Build Control Rig (FK + IK)", default=False,
        description="Build a posable rig automatically from the file: each "
                    "deform bone gets its matching Maya control shape (rotate "
                    "for FK) and the limbs get working IK targets. The Maya "
                    "node-graph logic (FK/IK blends, driven keys) is not "
                    "reproduced, but you can pose the character immediately")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="Orientation", icon='ORIENTATION_GLOBAL')
        box.prop(self, "global_scale")
        box.prop(self, "apply_axis")

        box = layout.box()
        box.label(text="Geometry", icon='MESH_DATA')
        box.prop(self, "import_meshes")
        sub = box.column()
        sub.enabled = self.import_meshes
        sub.prop(self, "import_blendshapes")
        sub.prop(self, "import_materials")
        box.prop(self, "hidden_handling")

        box = layout.box()
        box.label(text="Scene", icon='SCENE_DATA')
        box.prop(self, "import_cameras")
        box.prop(self, "import_lights")
        box.prop(self, "import_animation")

        box = layout.box()
        box.label(text="Rig", icon='ARMATURE_DATA')
        box.prop(self, "import_skeleton")
        sub = box.column()
        sub.enabled = self.import_skeleton
        sub.prop(self, "import_skin")
        sub.prop(self, "orient_bones")
        sub.prop(self, "experimental_rig")

    def execute(self, context):
        import time
        import traceback
        from . import scene_builder

        t0 = time.time()
        try:
            stats = scene_builder.build(self.filepath, self, report=self.report,
                                        parse=self.scene_parser())
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            traceback.print_exc()
            self.report({'ERROR'}, "Import failed (%s). %s"
                        % (exc, self.failure_hint))
            return {'CANCELLED'}

        self.report({'INFO'}, "Imported in %.1fs (%d meshes, %d bones)"
                    % (time.time() - t0, stats.get('meshes', 0),
                       stats.get('bones', 0)))
        return {'FINISHED'}


class EXPORT_SCENE_OT_maya_ascii(bpy.types.Operator, ExportHelper):
    """Export the scene to a Maya ASCII (.ma) file"""
    bl_idname = "export_scene.maya_ascii"
    bl_label = "Export Maya Scene (.ma)"
    bl_options = {'REGISTER', 'PRESET'}

    filename_ext = ".ma"
    filter_glob: StringProperty(default="*.ma", options={'HIDDEN'})

    use_selection: BoolProperty(
        name="Selection Only", default=False,
        description="Export only selected objects")
    export_scale: FloatProperty(
        name="Scale", default=100.0, min=1e-6, max=1e6,
        soft_min=0.01, soft_max=1000.0,
        description="Maya works in centimetres, so 100 converts Blender metres "
                    "to centimetres")
    apply_axis: BoolProperty(name="Z-up to Y-up", default=True,
                             description="Convert Blender Z-up to Maya Y-up")

    export_normals: BoolProperty(name="Normals", default=True)
    export_uvs: BoolProperty(name="UVs", default=True)
    export_colors: BoolProperty(name="Vertex Colors", default=True)
    export_materials: BoolProperty(name="Materials", default=True)
    export_skin: BoolProperty(name="Skin Weights", default=True)
    export_shapekeys: BoolProperty(name="Blend Shapes (Shape Keys)", default=True)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "use_selection")
        col = layout.column(heading="Orientation")
        col.prop(self, "export_scale")
        col.prop(self, "apply_axis")
        col = layout.column(heading="Include")
        col.prop(self, "export_normals")
        col.prop(self, "export_uvs")
        col.prop(self, "export_colors")
        col.prop(self, "export_materials")
        col.prop(self, "export_skin")
        col.prop(self, "export_shapekeys")

    def execute(self, context):
        import time
        import traceback
        from . import ma_exporter

        t0 = time.time()
        try:
            stats = ma_exporter.export(self.filepath, self, context,
                                       report=self.report)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.report({'ERROR'}, "Maya export failed: %s" % exc)
            return {'CANCELLED'}

        self.report({'INFO'}, "Maya exported in %.1fs (%d meshes, %d joints)"
                    % (time.time() - t0, stats.get('meshes', 0),
                       stats.get('joints', 0)))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Sidebar GUI (3D Viewport ▸ N ▸ "Maya IO")
# ---------------------------------------------------------------------------
class MayaIOSettings(bpy.types.PropertyGroup):
    """Quick settings shown in the sidebar; passed to the operators on launch."""
    # import
    imp_scale: FloatProperty(name="Scale", default=0.01, min=1e-6,
                             soft_min=0.001, soft_max=10.0,
                             description="0.01 = Maya cm to Blender m")
    imp_axis: BoolProperty(name="Y-up to Z-up", default=True)
    imp_hidden: EnumProperty(name="Hidden", default='HIDE', items=_HIDDEN_ITEMS)
    imp_experimental: BoolProperty(
        name="Build Control Rig (FK + IK)", default=False,
        description="Posable rig: control shapes on bones (FK) + limb IK")
    # export
    exp_scale: FloatProperty(name="Scale", default=100.0, min=1e-6,
                             soft_min=0.01, soft_max=1000.0,
                             description="100 = Blender m to Maya cm")
    exp_selection: BoolProperty(name="Selection Only", default=False)


class MAYAIO_PT_main(bpy.types.Panel):
    bl_label = "Maya Scene IO"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Maya IO"

    def draw(self, context):
        layout = self.layout
        s = context.scene.maya_io

        box = layout.box()
        box.label(text="Import", icon='IMPORT')
        col = box.column(align=True)
        sub = col.column(align=True)
        sub.prop(s, "imp_scale")
        sub.prop(s, "imp_axis")
        sub.prop(s, "imp_hidden", text="Hidden")
        sub.prop(s, "imp_experimental")
        row = box.row()
        row.scale_y = 1.5
        op = row.operator("import_scene.maya_ascii",
                          text="Import Maya Scene", icon='IMPORT')
        op.global_scale = s.imp_scale
        op.apply_axis = s.imp_axis
        op.hidden_handling = s.imp_hidden
        op.experimental_rig = s.imp_experimental

        box = layout.box()
        box.label(text="Export", icon='EXPORT')
        col = box.column(align=True)
        col.prop(s, "exp_scale")
        col.prop(s, "exp_selection")
        n_sel = len(context.selected_objects)
        n_all = len(context.scene.objects)
        box.label(text="%d object(s) will export"
                  % (n_sel if s.exp_selection else n_all),
                  icon='OBJECT_DATA')
        row = box.row()
        row.scale_y = 1.5
        op = row.operator("export_scene.maya_ascii",
                          text="Export Maya Scene", icon='EXPORT')
        op.export_scale = s.exp_scale
        op.use_selection = s.exp_selection


class MAYAIO_PT_about(bpy.types.Panel):
    bl_label = "About"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Maya IO"
    bl_parent_id = "MAYAIO_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text=".ma import and export, .mb import")
        col.label(text="Mesh, skeleton, skin, blend")
        col.label(text="shapes, materials, cameras,")
        col.label(text="lights & animation.")
        layout.label(text="Runs offline. No Maya needed.")


import_menu_text = "Maya Scene (.ma/.mb)"
menu_text = "Maya Scene (.ma)"        # an edition with more formats changes it


def menu_func_import(self, context):
    self.layout.operator(IMPORT_SCENE_OT_maya_ascii.bl_idname,
                         text=import_menu_text)


def menu_func_export(self, context):
    self.layout.operator(EXPORT_SCENE_OT_maya_ascii.bl_idname, text=menu_text)


classes = (
    MayaIOSettings,
    IMPORT_SCENE_OT_maya_ascii,
    EXPORT_SCENE_OT_maya_ascii,
    MAYAIO_PT_main,
    MAYAIO_PT_about,
)
_registered = ()


def register(edition_classes=classes):
    """Register ``edition_classes`` (one MayaIOSettings group among them)."""
    global _registered
    _registered = edition_classes
    for cls in edition_classes:
        bpy.utils.register_class(cls)
    settings = next(c for c in edition_classes
                    if issubclass(c, bpy.types.PropertyGroup))
    bpy.types.Scene.maya_io = PointerProperty(type=settings)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    del bpy.types.Scene.maya_io
    for cls in reversed(_registered):
        bpy.utils.unregister_class(cls)
