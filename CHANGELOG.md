# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/) and
[semantic versioning](https://semver.org/).

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
