# Contributing

Thanks for taking a look. Bug reports with a sample `.ma` that reproduces the
problem are the single most useful thing you can send, because the format has a
long tail of edge cases and real files are how they get found.

## How the code is laid out

The add-on is split so the hard part can be tested without Blender:

- `ma_parser.py` and `ma_model.py` do not import `bpy`. The parser turns a Maya
  ASCII file into a node and connection graph; the model turns that graph into
  plain Python data (meshes, skeleton, skin, blend shapes, materials, and so on).
- `scene_builder.py`, `ma_exporter.py`, `rig_builder.py`, `maya_bridge.py` and
  `__init__.py` are the Blender side and need `bpy`.

So if you are working on parsing or reconstruction, you can iterate with a normal
Python interpreter and never open Blender.

## Running the checks

```
python -m compileall maya_scene_importer

cd maya_scene_importer
python -c "import ma_parser, ma_model; print('imports ok')"
```

CI runs the same two checks plus a small parse-and-reconstruct smoke test on
every push and pull request.

## Building the add-on

```
blender --command extension build --source-dir maya_scene_importer --output-dir dist
```

Releases are built automatically: push a tag like `v1.1.0` and the release
workflow packages the zip and attaches it to a GitHub Release. Bump the version
in `maya_scene_importer/blender_manifest.toml` first.

## Style

- Plain Python, no third-party dependencies. The add-on must work with the
  Python that ships inside Blender.
- Keep `ma_parser.py` and `ma_model.py` free of `bpy`.
- Wrap anything that touches a single mesh, material, skin cluster or rig piece
  so one unsupported node never aborts a whole import.
- Four-space indent, lines under roughly 80 columns, matching the existing code.

## Scope

A reminder on what is and is not in scope, so PRs do not go sideways: the Maya
control-rig logic (FK/IK blends, set-driven keys, the utility-node graph) cannot
be reproduced in Blender and is out of scope by nature, not by choice. Geometry,
skeletons, skin, blend shapes, materials and broader file compatibility are all
fair game.
