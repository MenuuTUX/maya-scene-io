# SPDX-License-Identifier: GPL-3.0-or-later
"""
Maya Scene Importer (.ma)
=========================

Import Maya ASCII (.ma) scenes straight into Blender — no Maya, no FBX/USD
round-trip — reconstructing as much as the two applications' data models share:

* polygon meshes (vertices, polygons, all UV sets, hard/soft edges, tweaks)
* the complete joint skeleton at its exact Maya bind pose
* skin-cluster weights → vertex groups + an Armature modifier
* blend-shape targets → shape keys
* materials → Principled BSDF, auto-wired to the matching texture set
* Maya object visibility, organised into tidy collections
* (experimental) control-curve shapes, constraints and IK handles

See README.md for exactly what does and does not transfer between Maya and
Blender.
"""

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       PointerProperty, StringProperty)
from bpy_extras.io_utils import ImportHelper, ExportHelper


_HIDDEN_ITEMS = [
    ('HIDE', "Import as Hidden",
     "Import them hidden, in a separate collection (most faithful)"),
    ('SKIP_HELPERS', "Skip Rig Helpers",
     "Skip throwaway rig-construction meshes; keep real geometry hidden"),
    ('SHOW', "Import Visible", "Import everything fully visible"),
]
_FORMAT_ITEMS = [
    ('MA', "Maya ASCII (.ma)", "Native, no Maya required"),
    ('MB', "Maya Binary (.mb)", "Via local Maya (mayapy)"),
]
_METHOD_ITEMS = [
    ('AUTO', "Auto", "Native for .ma; use Maya for .mb. Best of both"),
    ('NATIVE', "Native (no Maya)",
     "Parse .ma directly. Fast, offline; standard files only"),
    ('MAYA', "Maya Bridge (any file)",
     "Let a local Maya convert any .ma/.mb to FBX, then import it. Most "
     "reliable for any file; requires Maya installed"),
]


class IMPORT_SCENE_OT_maya_ascii(bpy.types.Operator, ImportHelper):
    """Import a Maya ASCII (.ma) scene: meshes, skeleton, skin, blend shapes, materials"""
    bl_idname = "import_scene.maya_ascii"
    bl_label = "Import Maya ASCII (.ma)"
    bl_options = {'REGISTER', 'UNDO', 'PRESET'}

    filename_ext = ".ma"
    filter_glob: StringProperty(default="*.ma;*.mb", options={'HIDDEN'})

    # -- engine -----------------------------------------------------------
    import_method: EnumProperty(
        name="Method", default='AUTO',
        description="How to read the file", items=_METHOD_ITEMS)

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
    import_normals: BoolProperty(
        name="Custom Split Normals", default=False,
        description="Apply Maya's explicit normals (slower). Otherwise smooth "
                    "shading with hard edges from Maya edge hardness is used")
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
        box.label(text="Engine", icon='SETTINGS')
        box.prop(self, "import_method")
        if self.import_method in ('AUTO', 'MAYA'):
            from . import maya_bridge
            prefs = _prefs()
            if not maya_bridge.available(prefs.maya_path if prefs else ""):
                box.label(text="Maya not set (needed for .mb / Maya Bridge)",
                          icon='INFO')

        native = self.import_method in ('AUTO', 'NATIVE')
        box = layout.box()
        box.active = native
        box.label(text="Native options", icon='ORIENTATION_GLOBAL')
        box.prop(self, "global_scale")
        box.prop(self, "apply_axis")

        box = layout.box()
        box.active = native
        box.label(text="Geometry", icon='MESH_DATA')
        box.prop(self, "import_meshes")
        sub = box.column()
        sub.enabled = self.import_meshes
        sub.prop(self, "import_normals")
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
        sub.prop(self, "experimental_rig")

    def execute(self, context):
        import os
        import time
        import traceback

        path = self.filepath
        is_mb = path.lower().endswith(".mb")
        prefs = _prefs()
        mayapy = prefs.maya_path if prefs else ""

        # decide engine: the Maya FBX bridge is the reliable "any file" path;
        # native handles standard .ma offline
        from . import maya_bridge
        use_maya = (self.import_method == 'MAYA'
                    or (self.import_method == 'AUTO' and is_mb))
        if is_mb and not use_maya:
            use_maya = True               # .mb cannot be parsed natively

        t0 = time.time()
        tmp_ma = None
        if use_maya:
            if not maya_bridge.available(mayapy):
                self.report({'ERROR'},
                            "Maya was not found. Set the mayapy path in Edit > "
                            "Preferences > Add-ons > Maya Scene IO, or choose "
                            "Native for a standard .ma file.")
                return {'CANCELLED'}
            fbx = None
            try:
                fbx = maya_bridge.to_fbx(path, mayapy=mayapy)
            except Exception as exc:
                print("[Maya bridge] FBX route failed:\n%s" % exc)
                try:
                    ma = maya_bridge.mb_to_ma(path, mayapy=mayapy)
                except Exception as exc2:
                    self.report({'ERROR'}, "%s\n(.ma conversion also failed: "
                                "%s)" % (exc, exc2))
                    return {'CANCELLED'}
                # Maya's FBX plugin failed (common on old Maya); the converted
                # .ma still goes through the native importer.
                self.report({'WARNING'}, "Maya's FBX export failed; imported "
                            "through a converted .ma instead (see console).")
                path = ma
                tmp_ma = ma
                use_maya = False
            if fbx:
                try:
                    bpy.ops.import_scene.fbx(filepath=fbx,
                                             automatic_bone_orientation=True,
                                             use_custom_normals=True,
                                             ignore_leaf_bones=True)
                except Exception as exc:
                    traceback.print_exc()
                    self.report({'ERROR'}, "FBX import failed: %s" % exc)
                    return {'CANCELLED'}
                finally:
                    try:
                        os.remove(fbx)
                    except OSError:
                        pass
                self.report({'INFO'}, "Imported via Maya bridge in %.1fs"
                            % (time.time() - t0))
                return {'FINISHED'}

        # native path
        from . import scene_builder
        try:
            stats = scene_builder.build(path, self, report=self.report)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            traceback.print_exc()
            self.report({'ERROR'}, "Native import failed (%s). If the file uses "
                        "plugin nodes or is non-standard, try the Maya Bridge "
                        "method." % exc)
            return {'CANCELLED'}
        finally:
            if tmp_ma:
                maya_bridge.discard_temp(tmp_ma)

        self.report({'INFO'}, "Imported (native) in %.1fs (%d meshes, %d bones)"
                    % (time.time() - t0, stats.get('meshes', 0),
                       stats.get('bones', 0)))
        return {'FINISHED'}


class EXPORT_SCENE_OT_maya_ascii(bpy.types.Operator, ExportHelper):
    """Export the scene to a Maya ASCII (.ma) or Binary (.mb) file"""
    bl_idname = "export_scene.maya_ascii"
    bl_label = "Export Maya Scene (.ma/.mb)"
    bl_options = {'REGISTER', 'PRESET'}

    filename_ext = ".ma"
    filter_glob: StringProperty(default="*.ma;*.mb", options={'HIDDEN'})

    use_selection: BoolProperty(
        name="Selection Only", default=False,
        description="Export only selected objects")
    export_format: EnumProperty(
        name="Format", default='MA',
        description="Maya ASCII works without Maya; Binary (.mb) needs a local "
                    "Maya install configured in preferences",
        items=_FORMAT_ITEMS)

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
        layout.prop(self, "export_format")
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
        import os
        import time
        import traceback
        import tempfile
        from . import ma_exporter

        out = self.filepath
        want_mb = self.export_format == 'MB' or out.lower().endswith(".mb")
        ma_path = out
        if want_mb:
            fd, ma_path = tempfile.mkstemp(suffix=".ma")
            os.close(fd)

        t0 = time.time()
        try:
            stats = ma_exporter.export(ma_path, self, context, report=self.report)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.report({'ERROR'}, "Maya export failed: %s" % exc)
            return {'CANCELLED'}

        if want_mb:
            from . import maya_bridge
            prefs = _prefs()
            mb_out = out if out.lower().endswith(".mb") else os.path.splitext(out)[0] + ".mb"
            try:
                maya_bridge.convert(ma_path, mb_out,
                                    mayapy=prefs.maya_path if prefs else "")
            except Exception as exc:
                self.report({'ERROR'}, str(exc))
                return {'CANCELLED'}
            finally:
                try:
                    os.remove(ma_path)
                except OSError:
                    pass

        self.report({'INFO'}, "Maya exported in %.1fs (%d meshes, %d joints)"
                    % (time.time() - t0, stats.get('meshes', 0),
                       stats.get('joints', 0)))
        return {'FINISHED'}


class MayaSceneImporterPrefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    maya_path: StringProperty(
        name="Maya / mayapy Path", subtype='FILE_PATH', default="",
        description="Path to mayapy (or the Maya bin folder). Only needed for "
                    "Maya Binary (.mb) import/export; .ma needs no Maya")

    def draw(self, context):
        layout = self.layout
        layout.label(text=".ma import/export works with no Maya installed.")
        layout.label(text="Only for Maya Binary (.mb) or the Maya bridge: point")
        layout.label(text="this at mayapy, a program in Maya's bin folder")
        layout.label(text="(mayapy.exe on Windows). Picking the bin folder works too.")
        layout.prop(self, "maya_path")
        from . import maya_bridge
        found = maya_bridge.find_mayapy(self.maya_path)
        if found:
            layout.label(text="Maya found: %s" % found, icon='CHECKMARK')
        elif self.maya_path:
            layout.label(text="No mayapy at that path", icon='ERROR')
        else:
            layout.label(text="Maya not found automatically (.mb disabled)",
                         icon='INFO')


def _prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Sidebar GUI (3D Viewport ▸ N ▸ "Maya IO")
# ---------------------------------------------------------------------------
class MayaIOSettings(bpy.types.PropertyGroup):
    """Quick settings shown in the sidebar; passed to the operators on launch."""
    # import
    imp_method: EnumProperty(name="Method", default='AUTO', items=_METHOD_ITEMS)
    imp_scale: FloatProperty(name="Scale", default=0.01, min=1e-6,
                             soft_min=0.001, soft_max=10.0,
                             description="0.01 = Maya cm to Blender m")
    imp_axis: BoolProperty(name="Y-up to Z-up", default=True)
    imp_hidden: EnumProperty(name="Hidden", default='HIDE', items=_HIDDEN_ITEMS)
    imp_experimental: BoolProperty(
        name="Build Control Rig (FK + IK)", default=False,
        description="Posable rig: control shapes on bones (FK) + limb IK")
    # export
    exp_format: EnumProperty(name="Format", default='MA', items=_FORMAT_ITEMS)
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
        col.prop(s, "imp_method", text="Method")
        native = s.imp_method in ('AUTO', 'NATIVE')
        sub = col.column(align=True)
        sub.active = native
        sub.prop(s, "imp_scale")
        sub.prop(s, "imp_axis")
        sub.prop(s, "imp_hidden", text="Hidden")
        sub.prop(s, "imp_experimental")
        row = box.row()
        row.scale_y = 1.5
        op = row.operator("import_scene.maya_ascii",
                          text="Import Maya Scene", icon='IMPORT')
        op.import_method = s.imp_method
        op.global_scale = s.imp_scale
        op.apply_axis = s.imp_axis
        op.hidden_handling = s.imp_hidden
        op.experimental_rig = s.imp_experimental

        box = layout.box()
        box.label(text="Export", icon='EXPORT')
        col = box.column(align=True)
        col.prop(s, "exp_format", text="Format")
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
        op.export_format = s.exp_format
        op.export_scale = s.exp_scale
        op.use_selection = s.exp_selection


class MAYAIO_PT_binary(bpy.types.Panel):
    bl_label = "Maya Binary (.mb)"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Maya IO"
    bl_parent_id = "MAYAIO_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        from . import maya_bridge
        prefs = _prefs()
        path = prefs.maya_path if prefs else ""
        found = maya_bridge.find_mayapy(path)
        col = layout.column()
        if found:
            col.label(text="Maya detected", icon='CHECKMARK')
            col.label(text="(.mb import/export enabled)")
        else:
            col.label(text=".ma works with no Maya", icon='INFO')
            col.label(text="Set mayapy below for .mb:")
        if prefs:
            col.prop(prefs, "maya_path", text="")
        else:
            col.label(text="Set path in Add-on Preferences", icon='PREFERENCES')


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
        col.label(text="Two-way Maya .ma / .mb bridge")
        col.label(text="Mesh, skeleton, skin, blend")
        col.label(text="shapes, materials, cameras,")
        col.label(text="lights & animation.")
        layout.label(text=".ma needs no Maya. Offline.")


def menu_func_import(self, context):
    self.layout.operator(IMPORT_SCENE_OT_maya_ascii.bl_idname,
                         text="Maya Scene (.ma/.mb)")


def menu_func_export(self, context):
    self.layout.operator(EXPORT_SCENE_OT_maya_ascii.bl_idname,
                         text="Maya Scene (.ma/.mb)")


classes = (
    MayaIOSettings,
    IMPORT_SCENE_OT_maya_ascii,
    EXPORT_SCENE_OT_maya_ascii,
    MayaSceneImporterPrefs,
    MAYAIO_PT_main,
    MAYAIO_PT_binary,
    MAYAIO_PT_about,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.maya_io = PointerProperty(type=MayaIOSettings)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    del bpy.types.Scene.maya_io
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
