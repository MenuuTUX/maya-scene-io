# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/) and
[semantic versioning](https://semver.org/).

## Unreleased

### Fixed

- Maya bridge failing on older Maya (tested path: 2014). The FBX export now
  tries every FBX version the plugin accepts, and falls back to converting the
  file to `.ma` and importing it natively.
- Non-ASCII paths (for example Chinese folders) on Windows: paths are now
  written into the Maya script as escaped literals instead of being passed on
  the command line, where mayapy mangled them. Maya's output is decoded as
  UTF-8 with replacement, so a locale-encoded message can no longer hide the
  real error. Thanks to HeiShan for the report and patch.
- Temp files were left open, which stopped Maya writing to them on Windows.
- Maya errors are now summarised instead of dumping Maya's start-up log.
- Windows: exported `.ma` files are written with `\n` line endings, not `\r\n`.
- `.ma` files saved with a BOM, `\r\n` endings, or the Windows code page
  (older Maya) now parse correctly.
- Linux and case-sensitive macOS volumes: the texture referenced by Maya is now
  matched case-insensitively, as it is on Windows.
- Non-ASCII names (accents, CJK) are kept instead of replaced with `_`.
- Blend shape names are preserved on export instead of becoming `target_N`.

### Changed

- `mayapy` is found in any installed Maya version (not a fixed list) on
  Windows, macOS and Linux, and via `MAYA_LOCATION`. The preference accepts the
  file, the `bin` folder or the install folder.
- `mayapy` runs without a console window on Windows and without Blender's
  `PYTHONHOME` / `PYTHONPATH`.
- Rewrote the install and `.mb` setup instructions for newcomers.

## 1.0.0

First public release.

### Import

- Native `.ma` parser (no Maya required) and a Maya bridge for `.mb` and any
  non-standard file. Pick the engine per import; `Auto` chooses for you.
- Meshes: vertices, polygons, all UV sets, edge hardness, vertex colours, and
  per-face material assignment.
- Materials rebuilt as Principled BSDF, with the matching PBR textures wired up
  automatically when they sit next to the file.
- Joint skeleton imported at its exact bind pose. Deform bones are split into
  their own bone collection so the viewport stays readable.
- Skin weights as vertex groups plus an Armature modifier.
- Blend shapes as shape keys.
- Cameras, lights, and time-based animation.
- Maya object visibility preserved; the scene is sorted into collections.
- Optional FK + IK control rig generated from the file.

### Export

- Native `.ma` writer, and `.mb` via the Maya bridge.
- Meshes, UVs, normals, vertex colours, per-face materials, object hierarchy and
  visibility.
- Skeleton, skin weights, and blend shapes.

### Known limitations

- The Maya control-rig logic (FK/IK blends, set-driven keys, the utility-node
  graph) has no Blender equivalent and is not carried by any engine. The deform
  skeleton, skin, and blend shapes do come across.
- Without Maya, only `.ma` is supported, and only standard data. `.mb` and files
  with plugin nodes require the Maya bridge.
- The native parser was developed and tested against Maya 2023 files.
