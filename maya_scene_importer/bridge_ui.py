# SPDX-License-Identifier: GPL-3.0-or-later
"""
The Maya-bridge edition of the UI: the shared operators from :mod:`ui` plus
Maya Binary (.mb) export and the "Maya Bridge" engine, which drive a local
``mayapy`` through :mod:`maya_bridge`.
Not shipped in the native edition.
"""

import bpy
from bpy.props import EnumProperty, StringProperty

from . import maya_bridge, mb_parser, ui


def _prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def _mayapy():
    prefs = _prefs()
    return prefs.maya_path if prefs else ""


class IMPORT_SCENE_OT_maya_ascii(ui.IMPORT_SCENE_OT_maya_ascii):
    """Import a Maya scene (.ma or .mb), natively or through a local Maya"""
    bl_label = "Import Maya Scene (.ma/.mb)"

    failure_hint = ("If the file uses plugin nodes or is non-standard, try "
                    "the Maya Bridge method.")
    import_method: EnumProperty(
        name="Method", default='AUTO',
        description="How to read the file",
        items=[
            ('AUTO', "Auto",
             "Read .ma and .mb directly; for .mb, let Maya convert it first "
             "when Maya is installed"),
            ('NATIVE', "Native (no Maya)",
             "Read .ma and .mb directly. Fast, offline, old and new Maya"),
            ('MAYA', "Maya Bridge (any file)",
             "Let a local Maya convert any .ma/.mb to FBX, then import it. "
             "Handles plugin nodes and construction history; requires Maya"),
        ])

    def draw(self, context):
        box = self.layout.box()
        box.label(text="Engine", icon='SETTINGS')
        box.prop(self, "import_method")
        if (self.import_method == 'MAYA'
                and not maya_bridge.available(_mayapy())):
            box.label(text="Maya not set (needed for the Maya Bridge)",
                      icon='INFO')
        super().draw(context)

    def execute(self, context):
        mayapy = _mayapy()
        use_maya = self.import_method == 'MAYA' or (
            self.import_method == 'AUTO'
            and mb_parser.is_binary(self.filepath)
            and maya_bridge.available(mayapy))
        if not use_maya:
            return super().execute(context)

        import os
        import time
        import traceback

        if not maya_bridge.available(mayapy):
            self.report({'ERROR'},
                        "Maya was not found. Set the mayapy path in Edit > "
                        "Preferences > Get Extensions > Installed > "
                        "Maya Scene IO, or choose Native.")
            return {'CANCELLED'}
        t0 = time.time()
        try:
            fbx = maya_bridge.to_fbx(self.filepath, mayapy=mayapy)
        except Exception as exc:
            print("[Maya bridge] FBX route failed:\n%s" % exc)
            return self._import_converted(context, exc, mayapy)
        try:
            bpy.ops.import_scene.fbx(filepath=fbx,
                                     automatic_bone_orientation=True,
                                     use_custom_normals=True,
                                     ignore_leaf_bones=True)
        except Exception:
            traceback.print_exc()
            self.report({'WARNING'}, "Blender could not import Maya's FBX; "
                        "read the file directly instead (see console).")
            return super().execute(context)
        finally:
            try:
                os.remove(fbx)
            except OSError:
                pass
        self.report({'INFO'}, "Imported via Maya bridge in %.1fs"
                    % (time.time() - t0))
        return {'FINISHED'}

    def _import_converted(self, context, fbx_error, mayapy):
        """Maya's FBX plugin failed (common on old Maya): have Maya write a
        .ma instead and run that through the native importer."""
        try:
            ma = maya_bridge.mb_to_ma(self.filepath, mayapy=mayapy)
        except Exception as exc:
            print("[Maya bridge] .ma conversion failed too:\n%s" % exc)
            self.report({'WARNING'}, "Maya could not convert the file; "
                        "read it directly instead (see console).")
            return super().execute(context)
        self.report({'WARNING'}, "Maya's FBX export failed; imported "
                    "through a converted .ma instead (see console).")
        original = self.filepath
        self.filepath = ma
        try:
            return super().execute(context)
        finally:
            self.filepath = original
            maya_bridge.discard_temp(ma)


class EXPORT_SCENE_OT_maya_ascii(ui.EXPORT_SCENE_OT_maya_ascii):
    """Export the scene to a Maya ASCII (.ma) or Binary (.mb) file"""
    bl_label = "Export Maya Scene (.ma/.mb)"

    filter_glob: StringProperty(default="*.ma;*.mb", options={'HIDDEN'})
    export_format: EnumProperty(
        name="Format", default='MA',
        description="Maya ASCII works without Maya; Binary (.mb) needs a "
                    "local Maya install configured in preferences",
        items=[
            ('MA', "Maya ASCII (.ma)", "Native, no Maya required"),
            ('MB', "Maya Binary (.mb)", "Via local Maya (mayapy)"),
        ])

    def draw(self, context):
        self.layout.use_property_split = True
        self.layout.prop(self, "export_format")
        super().draw(context)

    def execute(self, context):
        out = self.filepath
        if self.export_format != 'MB' and not out.lower().endswith(".mb"):
            return super().execute(context)

        import os
        import tempfile

        fd, ma_path = tempfile.mkstemp(suffix=".ma")
        os.close(fd)
        mb_out = out if out.lower().endswith(".mb") \
            else os.path.splitext(out)[0] + ".mb"
        self.filepath = ma_path
        try:
            if super().execute(context) != {'FINISHED'}:
                return {'CANCELLED'}
            maya_bridge.convert(ma_path, mb_out, mayapy=_mayapy())
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        finally:
            self.filepath = out
            try:
                os.remove(ma_path)
            except OSError:
                pass
        return {'FINISHED'}


class MayaSceneImporterPrefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    maya_path: StringProperty(
        name="Maya / mayapy Path", subtype='FILE_PATH', default="",
        description="Path to mayapy (or the Maya bin folder). Only needed for "
                    "the Maya Bridge and for .mb export; .ma and .mb import "
                    "need no Maya")

    def draw(self, context):
        layout = self.layout
        layout.label(text=".ma import/export and .mb import work with no Maya.")
        layout.label(text="Only for .mb export or the Maya bridge: point")
        layout.label(text="this at mayapy, a program in Maya's bin folder")
        layout.label(text="(mayapy.exe on Windows). Picking the bin folder works too.")
        layout.prop(self, "maya_path")
        found = maya_bridge.find_mayapy(self.maya_path)
        if found:
            layout.label(text="Maya found: %s" % found, icon='CHECKMARK')
        elif self.maya_path:
            layout.label(text="No mayapy at that path", icon='ERROR')
        else:
            layout.label(text="Maya not found automatically (.mb export "
                         "disabled)", icon='INFO')


class MAYAIO_PT_binary(bpy.types.Panel):
    bl_label = "Maya Binary (.mb)"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Maya IO"
    bl_parent_id = "MAYAIO_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        prefs = _prefs()
        col = layout.column()
        if maya_bridge.find_mayapy(_mayapy()):
            col.label(text="Maya detected", icon='CHECKMARK')
            col.label(text="(.mb export enabled)")
        else:
            col.label(text=".ma and .mb import need no Maya", icon='INFO')
            col.label(text="Set mayapy below for .mb export:")
        if prefs:
            col.prop(prefs, "maya_path", text="")
        else:
            col.label(text="Set path in Add-on Preferences", icon='PREFERENCES')


classes = (
    ui.MayaIOSettings,
    IMPORT_SCENE_OT_maya_ascii,
    EXPORT_SCENE_OT_maya_ascii,
    MayaSceneImporterPrefs,
    ui.MAYAIO_PT_main,
    MAYAIO_PT_binary,
    ui.MAYAIO_PT_about,
)


def register():
    ui.menu_text = "Maya Scene (.ma/.mb)"
    ui.register(classes)


def unregister():
    ui.unregister()
