# SPDX-License-Identifier: GPL-3.0-or-later
"""
Optional Maya bridge.

Native `.ma` import/export needs no Maya. But Maya **Binary** (`.mb`) is an
undocumented proprietary format that cannot be parsed reliably without Autodesk
Maya. When a local Maya is available, this module drives ``mayapy`` headlessly
to convert `.mb` <-> `.ma`, so the add-on also handles `.mb` (and can produce
maximum-fidelity round-trips) — while still working fully offline for `.ma`.

This mirrors how the Maya-dependent commercial importers work, but here it is an
optional enhancement rather than a hard requirement.
"""

import os
import subprocess
import tempfile


_COMMON_MAYAPY = [
    # Windows
    r"C:/Program Files/Autodesk/Maya{ver}/bin/mayapy.exe",
    # macOS
    "/Applications/Autodesk/maya{ver}/Maya.app/Contents/bin/mayapy",
    # Linux
    "/usr/autodesk/maya{ver}/bin/mayapy",
]
_VERSIONS = ["2025", "2024", "2023", "2022", "2020", "2019", "2018"]


def find_mayapy(explicit=""):
    """Return a usable mayapy path, or '' if none found."""
    if explicit:
        explicit = bpy_path_abs(explicit)
        if os.path.isfile(explicit):
            return explicit
        # allow pointing at the Maya bin folder
        for exe in ("mayapy.exe", "mayapy"):
            cand = os.path.join(explicit, exe)
            if os.path.isfile(cand):
                return cand
    for tmpl in _COMMON_MAYAPY:
        for ver in _VERSIONS:
            cand = tmpl.format(ver=ver)
            if os.path.isfile(cand):
                return cand
    return ""


def bpy_path_abs(p):
    try:
        import bpy
        return bpy.path.abspath(p)
    except Exception:
        return os.path.abspath(os.path.expanduser(p))


def available(explicit=""):
    return bool(find_mayapy(explicit))


_CONVERT_SCRIPT = r"""
import sys
import maya.standalone
maya.standalone.initialize(name="python")
import maya.cmds as cmds
src, dst, dst_type = sys.argv[1], sys.argv[2], sys.argv[3]
cmds.file(src, open=True, force=True, prompt=False, loadReferenceDepth="all")
cmds.file(rename=dst)
cmds.file(save=True, type=dst_type, force=True)
try:
    maya.standalone.uninitialize()
except Exception:
    pass
"""


def convert(src, dst, mayapy="", timeout=600):
    """Convert between Maya formats using mayapy. ``dst`` extension decides the
    output type (.ma -> mayaAscii, .mb -> mayaBinary). Returns dst on success;
    raises RuntimeError otherwise."""
    exe = find_mayapy(mayapy)
    if not exe:
        raise RuntimeError(
            "Autodesk Maya (mayapy) was not found. Set its path in the add-on "
            "preferences to enable .mb support; .ma works without Maya.")
    dst_type = "mayaBinary" if dst.lower().endswith(".mb") else "mayaAscii"
    fd, script = tempfile.mkstemp(suffix=".py", text=True)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(_CONVERT_SCRIPT)
        proc = subprocess.run(
            [exe, script, src, dst, dst_type],
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 or not os.path.isfile(dst):
            raise RuntimeError("mayapy conversion failed:\n%s\n%s"
                               % (proc.stdout[-2000:], proc.stderr[-2000:]))
        return dst
    finally:
        try:
            os.remove(script)
        except OSError:
            pass


def mb_to_ma(src_mb, mayapy=""):
    """Convert a .mb to a temporary .ma and return its path."""
    tmp = tempfile.mkstemp(suffix=".ma")[1]
    return convert(src_mb, tmp, mayapy=mayapy)


# ---------------------------------------------------------------------------
# Maya FBX bridge: the reliable "works with any file" path.
#
# Maya itself reads every node type, plugin, version and reference, so letting
# Maya export the scene to FBX (then importing that with Blender's native FBX
# importer) handles any .ma/.mb file far more robustly than parsing it by hand.
# ---------------------------------------------------------------------------
_FBX_EXPORT_SCRIPT = r"""
import sys
import maya.standalone
maya.standalone.initialize(name="python")
import maya.cmds as cmds
import maya.mel as mel

src, dst, bake = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
cmds.file(src, open=True, force=True, prompt=False, loadReferenceDepth="all")
if not cmds.pluginInfo("fbxmaya", q=True, loaded=True):
    cmds.loadPlugin("fbxmaya")

def fbx(cmd):
    try:
        mel.eval(cmd)
    except Exception:
        pass

fbx('FBXResetExport')
fbx('FBXExportSmoothingGroups -v true')
fbx('FBXExportSmoothMesh -v false')
fbx('FBXExportTangents -v true')
fbx('FBXExportTriangulate -v false')
fbx('FBXExportSkins -v true')
fbx('FBXExportShapes -v true')          # blend shapes
fbx('FBXExportSkeletonDefinitions -v true')
fbx('FBXExportCameras -v true')
fbx('FBXExportLights -v true')
fbx('FBXExportInputConnections -v false')
fbx('FBXExportConstraints -v false')
fbx('FBXExportInAscii -v false')
fbx('FBXExportUpAxis y')
fbx('FBXExportFileVersion -v FBX202000')
fbx('FBXExportBakeComplexAnimation -v %s' % ('true' if bake else 'false'))
if bake:
    start = cmds.playbackOptions(q=True, min=True)
    end = cmds.playbackOptions(q=True, max=True)
    fbx('FBXExportBakeComplexStart -v %d' % int(start))
    fbx('FBXExportBakeComplexEnd -v %d' % int(end))

cmds.file(dst, force=True, type="FBX export", exportAll=True,
          preserveReferences=False, prompt=False)
try:
    maya.standalone.uninitialize()
except Exception:
    pass
"""


def to_fbx(src, mayapy="", bake_animation=True, timeout=1800):
    """Convert any .ma/.mb to a temporary FBX via Maya. Returns the FBX path."""
    exe = find_mayapy(mayapy)
    if not exe:
        raise RuntimeError(
            "Autodesk Maya (mayapy) was not found. The reliable 'any file' "
            "path needs a local Maya. Set its path in the add-on preferences. "
            "Native .ma import works without Maya for standard files.")
    dst = tempfile.mkstemp(suffix=".fbx")[1]
    fd, script = tempfile.mkstemp(suffix=".py", text=True)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(_FBX_EXPORT_SCRIPT)
        proc = subprocess.run(
            [exe, script, src, dst, "1" if bake_animation else "0"],
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 or not os.path.isfile(dst) \
                or os.path.getsize(dst) == 0:
            raise RuntimeError("Maya FBX export failed:\n%s\n%s"
                               % (proc.stdout[-2000:], proc.stderr[-2000:]))
        return dst
    finally:
        try:
            os.remove(script)
        except OSError:
            pass
