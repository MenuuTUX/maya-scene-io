# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/) and
[semantic versioning](https://semver.org/).

## Unreleased

## 1.1.0 - 2026-11-02

### Fixed

- Animation was keyed only on Maya's key frames, and Blender blended location
  and rotation separately in between. A door turning about its hinge drifted
  off it mid-swing (up to 5 m in the Lantana sample). Every frame of the key
  range is now keyed from the full Maya matrix.
- Curves were sampled as straight lines. They now follow each key's Maya
  tangent type: linear, flat, stepped, spline, and the smooth auto, clamped
  and plateau types, which ease in and out without overshooting.
- A mesh under an animated group (a door and its handle in a `DoorSet`) did
  not move, and an animated group with no mesh of its own was skipped.
- Objects were built from translate, rotate and scale only. Maya's rotate and
  scale pivots, the pivot translates it adds when a pivot moves, the rotate
  axis and shear were ignored, so any object with a moved pivot landed in the
  wrong place, sometimes metres away. In the Lantana sample 6,057 of 10,231
  transforms carry a pivot translate. Transforms now use Maya's full matrix,
  for `.ma` and `.mb` alike.
- An animated scene opened on its first frame instead of the frame it was
  saved on (`time1.outTime`), so animated objects such as drawers sat in a
  different pose than in Maya.
- A `.ma` saved with a working unit other than centimetres or degrees
  (`currentUnit -l meter`, `inch`, `-a radian`, ...) came in at the wrong size
  and rotation: translations, joint orients, camera clip planes, spot cone
  angles and animation keys were read as cm and degrees. They are now converted
  from the scene's units. Mesh points, matrices and curve CVs were already
  right, since Maya writes those in centimetres whatever the working unit.
- Vertex tweaks written as a range (`.pt[0:7]`, the usual form) were ignored,
  so tweaked meshes landed in the wrong place. Only single-vertex tweaks were
  applied.
- Objects in a Maya display layer that is switched off were imported visible
  (for example the `bangs` mesh in the sample rig). They now follow the layer
  and are imported hidden, like Maya shows them.
- A stray `"` in a damaged file swallowed the rest of the file and made large
  files take minutes to read. Only the damaged statement is lost now.
- One damaged mesh, skin, shape key set, material or joint no longer aborts the
  whole import. Each is skipped or cut short and listed in a warning, as are
  non-finite numbers and values with missing components.
- A joint chain more than about 1000 joints deep (a tail or rope rig) hit
  Python's recursion limit, and a parent loop in a damaged file could spin.
- Texture folder listings were cached for the whole Blender session, so adding
  a texture and importing again still missed it.
- Importing while another object was in Edit Mode could fail at the armature.
- Exporter: a mirrored (negative scale) object came back inside out. Normals
  were written in object space instead of world space. Only the active UV set
  was written, renamed `map1`. Shape key and skin data was written against the
  wrong vertices when a modifier changed the vertex count; it is now skipped
  with a warning. Shape key values were dropped; they are now blendShape weights.
- The "Custom Split Normals" import option never did anything. It is hidden
  (saved presets still load) and the README no longer lists normals as imported.
- Joint names longer than Blender's 63-byte limit (common with namespaced rigs)
  were cut by Blender, which detached them from their skin weights, animation
  and IK. Long names now get a short hash so they stay unique and linked.
- Two nodes with the same short name under different groups (Maya writes their
  `|group|node` paths) were merged; each now keeps its own parent, material and
  connections.
- A zero scale or other singular matrix on one animated object stopped all
  animation import; that object is now skipped with a warning.
- Material assignments went to the wrong faces when a mesh had faces dropped
  for invalid indices.
- Damaged input: a truncated file keeps everything before the damage and says
  so, a Maya Binary file saved with a `.ma` name and an empty or non-Maya file
  get a clear error, and referenced scenes that are not loaded are listed in a
  warning.
- Skin weights were incomplete when Maya split one vertex's weight list across
  two `setAttr` blocks. Affected vertices lost part of their weight and
  deformed wrongly; the lists are now merged.
- Blend shape keys were all imported at full strength. They now start at the
  weight stored in the file (usually 0). In-between targets no longer replace
  the full target.
- Maya transparency (`.it`) was read as emission, so glass glowed and
  incandescent materials (`.ic`) did not. Transparency now becomes alpha.
- Meshes ignored their transform node. Static meshes now land at their world
  position, mirrored transforms keep the right face winding, and a mesh parented
  under a joint follows that bone.
- Animation import: translation and rotation keys are now converted through
  joint orient, rotate order, the unit scale and the Y-up to Z-up swap, and
  playback rate and frame range come from the file.
- Cameras and lights were created with a 0.01 object scale.
- Exporter: the mesh is written in its rest pose on its basis shape instead of
  whatever pose or shape key mix was active, and per-face material groups
  survive an export and re-import.
- Rig option: IK targets sit on the end joint (wrist, ankle) and the chain
  reaches it, instead of stopping at the elbow or knee.

### Changed

- Faster import: meshes, UVs, sharp edges and shape keys are written to Blender
  in bulk (mesh building about 2.5 times faster). The `.ma` reader streams the
  file and keeps large number lists as text until they are needed, so reading
  and decoding a 46 MB scene takes about a third of the time and memory, and
  connection lookups no longer scan every connection.
- The Blender Extensions edition (`scripts/build_native_extension.py`) no longer
  carries any Maya bridge, `mayapy` path or `.mb` code. The reviewers declined
  the previous package because it still contained them, even though they were
  switched off. The operators and sidebar now live in `ui.py`, shared by both
  editions; the full edition adds `bridge_ui.py` on top. In the full edition the
  sidebar no longer has Method and Format drop-downs; choose them in the file
  dialog.

### Added

- Maya Binary (`.mb`) import with no Maya installed. A new pure-Python reader
  (`mb_parser.py`) handles 64-bit (`FOR8`, Maya 2014 and later) and 32-bit
  (`FOR4`, older Maya) files. It reads meshes with UVs and per-face materials,
  transforms, joints, skin weights, NURBS curves, cameras, display layers and
  animation curves. Blend shapes, vertex colours and creases are not read from
  `.mb` yet. Before this, `.mb` import failed with "Maya was not found"
  unless Maya was installed. `Auto` still uses the Maya bridge for `.mb` when
  Maya is installed, and falls back to the native reader if the bridge fails.
  A binary scene saved with a `.ma` name is detected and read too. Both
  editions include it, since it needs no other application.
- A warning for each mesh skipped because the file stores only the
  construction history that builds it (such as a `polyCube`), instead of
  dropping it without a word.

- Vertex colours: colour sets are imported as per-corner colour attributes, and
  exported with the face-to-colour records Maya needs to attach them.
- Textures are found from the path the Maya file node names, in the scene's
  folder and sourceimages, textures, images and maps folders, in any letter
  case. A `bump2d` becomes a normal map or a bump node as Maya set it, and an
  incandescence map is wired to emission.
- Cameras keep their sensor size, clip planes and orthographic width.
- A progress bar while a scene imports.
- Bones now point at their child, as Blender draws a skeleton. Deformation is
  unchanged and the exporter undoes the rotation. Turn off "Orient Bones to
  Children" to keep Maya's joint axes.

## 1.0.1

### Fixed

- Vertex tweaks written as a range (`.pt[0:7]`, the usual form) were ignored,
  so tweaked meshes landed in the wrong place. Only single-vertex tweaks were
  applied.
- Maya bridge failing on older Maya. The FBX export now
  tries every FBX version the plugin accepts, and falls back to converting the
  file to `.ma` and importing it natively.
- Non-ASCII paths (for example Chinese folders) on Windows: paths are now
  written into the Maya script as escaped literals instead of being passed on
  the command line, where mayapy mangled them. Maya's output is decoded as
  UTF-8 with replacement, so a locale-encoded message can no longer hide the
  real error. Thanks to HeiShan for the report and patch.
- Temp files go to an ASCII-only folder when the user temp path is not (non-ASCII Windows user names).
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
