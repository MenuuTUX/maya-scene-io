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

import glob
import locale
import os
import re
import subprocess
import sys
import tempfile


def _version_key(path):
    """Sort key so the newest Maya (highest year in the path) comes first."""
    m = re.findall(r"(?:maya|Maya)(\d{4})", path)
    return -int(m[-1]) if m else 0


def _install_globs():
    if sys.platform.startswith("win"):
        roots = {os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramW6432", r"C:\Program Files")}
        return [os.path.join(r, "Autodesk", "Maya*", "bin", "mayapy.exe")
                for r in roots]
    if sys.platform == "darwin":
        return ["/Applications/Autodesk/maya*/Maya.app/Contents/bin/mayapy"]
    return ["/usr/autodesk/maya*/bin/mayapy",
            "/opt/autodesk/maya*/bin/mayapy",
            "/usr/autodesk/maya*-x64/bin/mayapy"]


def installed_mayapys():
    """Every mayapy found in the standard install locations, newest first."""
    found = []
    loc = os.environ.get("MAYA_LOCATION")
    if loc:
        found += glob.glob(os.path.join(loc, "bin", "mayapy*"))
        found += glob.glob(os.path.join(loc, "Contents", "bin", "mayapy"))
    for pattern in _install_globs():
        found += glob.glob(pattern)
    found = [f for f in dict.fromkeys(found) if os.path.isfile(f)]
    return sorted(found, key=_version_key)


def _resolve(path):
    """Turn whatever the user picked into a mayapy file, or ''."""
    path = os.path.normpath(path)
    if os.path.isfile(path):
        name = os.path.basename(path).lower()
        if name.startswith("mayapy"):
            return path
        # they picked maya.exe / Maya.app's binary: mayapy sits beside it
        for exe in ("mayapy.exe", "mayapy"):
            cand = os.path.join(os.path.dirname(path), exe)
            if os.path.isfile(cand):
                return cand
        return ""
    # a folder: the bin folder, the Maya install root, or a macOS Maya.app
    for sub in ("", "bin", os.path.join("Contents", "bin")):
        for exe in ("mayapy.exe", "mayapy"):
            cand = os.path.join(path, sub, exe)
            if os.path.isfile(cand):
                return cand
    return ""


def find_mayapy(explicit=""):
    """Return a usable mayapy path, or '' if none found."""
    if explicit.strip():
        found = _resolve(bpy_path_abs(explicit.strip()))
        if found:
            return found
    found = installed_mayapys()
    return found[0] if found else ""


def bpy_path_abs(p):
    try:
        import bpy
        return bpy.path.abspath(p)
    except Exception:
        return os.path.abspath(os.path.expanduser(p))


def available(explicit=""):
    return bool(find_mayapy(explicit))


def _tmp_dir():
    """A temp folder whose path is pure ASCII when one exists. Maya reads MEL
    strings in the system code page, so an FBX written under a non-ASCII
    Windows user folder can fail; Public/ProgramData/tmp are safe."""
    base = tempfile.gettempdir()
    if base.isascii():
        return base
    for cand in (os.environ.get("PUBLIC"), os.environ.get("ProgramData"),
                 "/tmp"):
        if cand and cand.isascii() and os.path.isdir(cand):
            try:
                path = os.path.join(cand, "mayaio_tmp")
                os.makedirs(path, exist_ok=True)
                return path
            except OSError:
                pass
    return base


def _tmp_path(suffix):
    """A temp file path with the descriptor closed (Windows cannot overwrite a
    file that is still open)."""
    fd, path = tempfile.mkstemp(suffix=suffix, dir=_tmp_dir())
    os.close(fd)
    return path


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _summarise(out, err):
    """Pull the real error out of Maya's startup chatter."""
    lines = ((out or "") + "\n" + (err or "")).splitlines()
    keep = [l for l in lines
            if l.startswith(("MAYAIO", "Error", "Traceback", "RuntimeError",
                             "  File", "// Error", "# Error"))
            or "Error:" in l]
    text = "\n".join(keep[-15:]).strip()
    return text or "\n".join(lines[-8:]).strip() or "(no output)"


def _decode(data):
    """Maya prints in the system code page (GBK, cp1252, ...) even when asked
    for UTF-8, so try UTF-8 first, then the locale, and never raise."""
    data = data or b""
    for enc in ("utf-8", locale.getpreferredencoding(False)):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return data.decode("utf-8", errors="replace")


def _literal(value):
    """An ASCII-only Python literal that is valid in Maya's Python 2 and 3."""
    if isinstance(value, str):
        return "u" + ascii(value)
    return repr(value)


def _run(exe, script_text, values, timeout):
    """Run a Python script under mayapy; returns the CompletedProcess.

    ``values`` become variables at the top of the script. They are NOT passed on
    the command line: on Windows mayapy re-reads its arguments through the ANSI
    code page, so a non-ASCII path (Chinese, accented, ...) arrives mangled and
    Maya reports the file as missing. ASCII-escaped literals are immune to that.
    """
    head = "".join("%s = %s\n" % (k, _literal(v)) for k, v in values.items())
    fd, script = tempfile.mkstemp(suffix=".py", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(head + script_text)
        # Blender's own Python variables would break Maya's interpreter
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP")}
        env["PYTHONIOENCODING"] = "utf-8"
        kw = {}
        if sys.platform.startswith("win"):
            kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
        try:
            proc = subprocess.run([exe, script], env=env,
                                  capture_output=True, timeout=timeout, **kw)
            proc.stdout = _decode(proc.stdout)
            proc.stderr = _decode(proc.stderr)
            return proc
        except subprocess.TimeoutExpired:
            raise RuntimeError("Maya took longer than %d s and was stopped."
                               % timeout)
        except OSError as exc:
            raise RuntimeError("Could not start mayapy (%s): %s" % (exe, exc))
    finally:
        _remove(script)


_NO_MAYA = ("Autodesk Maya (mayapy) was not found. Set its path in "
            "Preferences > Add-ons > Maya Scene IO (see the README), or use "
            "Native for a standard .ma file.")

# Every Maya script shares this wrapper: it prints one clear MAYAIO line on
# failure and exits non-zero, so the add-on can show it instead of Maya's logs.
_PRELUDE = r"""
import sys, traceback
def _fail(msg):
    sys.stdout.write("\nMAYAIO_ERROR: %s\n" % msg)
    sys.stdout.write(traceback.format_exc() + "\n")
    sys.stdout.flush()
    sys.exit(1)
try:
    import maya.standalone
    maya.standalone.initialize(name="python")
    import maya.cmds as cmds
    import maya.mel as mel
except Exception:
    _fail("could not start Maya (is this really mayapy?)")

def open_scene(src):
    try:
        cmds.file(src, open=True, force=True, prompt=False,
                  loadReferenceDepth="all")
    except (TypeError, RuntimeError):
        # older Maya, or a missing reference: retry plainly
        cmds.file(src, open=True, force=True, prompt=False)

def finish():
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
        raise RuntimeError(_NO_MAYA)
    dst_type = "mayaBinary" if dst.lower().endswith(".mb") else "mayaAscii"
    body = _PRELUDE + r"""
try:
    open_scene(src)
    cmds.file(rename=dst)
    cmds.file(save=True, type=dst_type, force=True)
except Exception:
    _fail("Maya could not convert the file")
finish()
"""
    proc = _run(exe, body, {"src": src, "dst": dst, "dst_type": dst_type},
                timeout)
    if proc.returncode != 0 or not os.path.isfile(dst) \
            or os.path.getsize(dst) == 0:
        raise RuntimeError("Maya conversion failed:\n%s"
                           % _summarise(proc.stdout, proc.stderr))
    return dst


def mb_to_ma(src_mb, mayapy=""):
    """Convert a .mb to a temporary .ma and return its path."""
    # keep the original name so the imported objects are named after the file
    folder = tempfile.mkdtemp(prefix="mayaio_", dir=_tmp_dir())
    tmp = os.path.join(folder, os.path.splitext(os.path.basename(src_mb))[0]
                       + ".ma")
    try:
        return convert(src_mb, tmp, mayapy=mayapy)
    except Exception:
        discard_temp(tmp)
        raise


def discard_temp(path):
    """Delete a file made by mb_to_ma along with its private folder."""
    _remove(path)
    try:
        os.rmdir(os.path.dirname(path))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Maya FBX bridge: the reliable "works with any file" path.
#
# Maya itself reads every node type, plugin, version and reference, so letting
# Maya export the scene to FBX (then importing that with Blender's native FBX
# importer) handles any .ma/.mb file far more robustly than parsing it by hand.
# ---------------------------------------------------------------------------
_FBX_EXPORT_BODY = r"""
try:
    open_scene(src)
    if not cmds.pluginInfo("fbxmaya", q=True, loaded=True):
        cmds.loadPlugin("fbxmaya")
except Exception:
    _fail("Maya could not open the scene or load its FBX plugin")

def fbx(cmd):
    try:
        mel.eval(cmd)
        return True
    except Exception:
        return False          # option unknown to this Maya's FBX plugin

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
# newest FBX version this Maya understands (2014-era plugins stop at 2013)
for ver in ("FBX202000", "FBX201900", "FBX201800", "FBX201600",
            "FBX201400", "FBX201300", "FBX201200"):
    if fbx('FBXExportFileVersion -v %s' % ver):
        break
fbx('FBXExportBakeComplexAnimation -v %s' % ('true' if bake else 'false'))
if bake:
    start = cmds.playbackOptions(q=True, min=True)
    end = cmds.playbackOptions(q=True, max=True)
    fbx('FBXExportBakeComplexStart -v %d' % int(start))
    fbx('FBXExportBakeComplexEnd -v %d' % int(end))

try:
    mel.eval('FBXExport -f "%s"' % dst.replace("\\", "/"))
except Exception:
    try:
        cmds.file(dst, force=True, type="FBX export", exportAll=True,
                  preserveReferences=False, prompt=False)
    except Exception:
        _fail("Maya %s could not export FBX" % cmds.about(version=True))
finish()
"""


def to_fbx(src, mayapy="", bake_animation=True, timeout=1800):
    """Convert any .ma/.mb to a temporary FBX via Maya. Returns the FBX path."""
    exe = find_mayapy(mayapy)
    if not exe:
        raise RuntimeError(_NO_MAYA)
    dst = _tmp_path(".fbx")
    try:
        proc = _run(exe, _PRELUDE + _FBX_EXPORT_BODY,
                    {"src": src, "dst": dst, "bake": bool(bake_animation)},
                    timeout)
        if proc.returncode != 0 or os.path.getsize(dst) == 0:
            raise RuntimeError("Maya FBX export failed:\n%s"
                               % _summarise(proc.stdout, proc.stderr))
        return dst
    except Exception:
        _remove(dst)
        raise
