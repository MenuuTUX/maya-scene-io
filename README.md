# Maya Scene IO

![Blender 4.2+](https://img.shields.io/badge/Blender-4.2%2B-orange)
![License GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue)

A Blender add-on for moving Maya scenes into Blender, and back out again. It
reads and writes Maya ASCII (`.ma`) natively, and handles Maya Binary (`.mb`)
plus anything non-standard by driving a local Maya install.

Which way you bring a file in depends on whether you have Maya installed, so
there are two engines and you pick one per import.

## Two engines

Reading *any* Maya file without Maya is not possible. `.mb` is an undocumented
binary format, and `.ma` files can reference plugin nodes (Arnold, custom rigs,
and so on) that no outside tool can decode. So the add-on gives you a choice:

**Native** parses `.ma` directly in Python. No Maya, works offline, fast. It
reliably handles the standard data (meshes, UVs, normals, the joint skeleton,
skin weights, blend shapes, and lambert/blinn/phong materials) on most files.
It is best effort: unusual nodes or exotic materials may come in partially.

**Maya bridge** points the add-on at a local `mayapy` and lets Maya export the
scene to FBX, which Blender then imports. Because Maya reads the file, this
works for any version, any plugin, references, and `.mb`. It needs Maya
installed, and it is the same approach the commercial Maya importers use.

`Auto` (the default) uses Native for `.ma` and the bridge for `.mb`.

## What comes across

Both engines bring over geometry, UVs, the skeleton at its bind pose, skin
weights, and blend shapes. The native engine also rebuilds materials as
Principled BSDF and wires up PBR textures sitting next to the file.

What does not transfer, by any tool or format, is the Maya control rig's logic:
the FK/IK switches, set-driven keys, and utility-node graph that make controls
drive joints. That lives in Maya's evaluation engine and has no Blender
equivalent. You get a clean, skinned deform skeleton; the animator rig is rebuilt
in Blender. The add-on can generate a basic FK + IK control rig from the file to
get you started.

## Features

Import:

- Meshes: vertices, polygons, all UV sets, edge hardness, per-face materials
- Materials as Principled BSDF with PBR textures auto-wired
- Joint skeleton at bind pose, deform bones split into their own bone collection
- Skin weights as vertex groups plus an Armature modifier
- Blend shapes as shape keys
- Cameras, lights, and time-based animation
- Maya object visibility, sorted into collections
- Optional FK + IK control rig generated from the file

Export:

- Meshes, UVs, normals, vertex colours, per-face materials, hierarchy, visibility
- Skeleton, skin weights, and blend shapes

## Install

Works the same on Windows, macOS and Linux. You need Blender 4.2 or newer.

1. Download `maya_scene_importer-x.y.z.zip` from the Releases page. **Do not
   unzip it.** Blender installs the zip as-is.
2. In Blender open `Edit > Preferences > Get Extensions`, click the small
   drop-down arrow (top right), choose `Install from Disk...`, and pick the zip.
3. Make sure the add-on is ticked in `Edit > Preferences > Add-ons`.

## Using it

Import from `File > Import > Maya Scene`, export from `File > Export > Maya
Scene`. There is also a "Maya IO" tab in the 3D viewport sidebar (press N) with
the same options and one-click buttons.

### Plain `.ma` files

Nothing else to set up. Pick `Native` (or leave it on `Auto`) and import.

### `.mb` files and the Maya bridge

These need Maya installed on the same computer. The add-on runs a small program
that ships with Maya called `mayapy` (it is a Python interpreter inside Maya's
`bin` folder). It never opens Maya's window.

The add-on looks for Maya in the usual places and picks the newest version it
finds, so you may not have to do anything. If the sidebar's "Maya Binary (.mb)"
panel says Maya was not found, set the path yourself in
`Edit > Preferences > Add-ons > Maya Scene IO`. You can pick any of these:

| OS | `mayapy` is usually at |
| --- | --- |
| Windows | `C:\Program Files\Autodesk\Maya2024\bin\mayapy.exe` |
| macOS | `/Applications/Autodesk/maya2024/Maya.app/Contents/bin/mayapy` |
| Linux | `/usr/autodesk/maya2024/bin/mayapy` |

Picking the `bin` folder, or the Maya install folder, works as well. The
`MAYA_LOCATION` environment variable is honoured too.

## Troubleshooting

- **"Maya was not found"**: set the path as described above.
- **"Maya FBX export failed"**: some older Maya versions (2014 and similar) ship
  an FBX plugin that rejects the export. The add-on then converts the file to
  `.ma` with Maya and imports that natively, so you normally still get your
  scene; a warning tells you this happened.
- **Anything else**: on Windows open `Window > Toggle System Console` in Blender
  (on macOS and Linux start Blender from a terminal), repeat the import, and
  attach the printed text to a [bug report](https://github.com/MenuuTUX/maya-scene-io/issues) together with your
  Maya and Blender versions.

## Limitations

- No Maya means `.ma` only, standard files only. `.mb` and files with plugin
  nodes need the Maya bridge.
- The control-rig logic does not transfer (see above).
- The native parser was developed against Maya 2023. Other versions should be
  fine for geometry and skeletons, since those formats are stable, but please
  open an issue if something breaks.
- Materials beyond lambert/blinn/phong (Arnold aiStandardSurface and friends)
  import as a plain material on the native path. Use the Maya bridge for those.

## Building from source

The add-on lives in `maya_scene_importer/`. Package it with:

```
blender --command extension build --source-dir maya_scene_importer --output-dir dist
```

## Layout

```
maya_scene_importer/
  __init__.py        operators, sidebar panel, preferences
  ma_parser.py       Maya ASCII tokeniser and node graph
  ma_model.py        turns the node graph into Blender-ready data
  scene_builder.py   builds the Blender scene on import
  ma_exporter.py     writes Maya ASCII on export
  maya_bridge.py     drives mayapy for .mb and FBX
  rig_builder.py     generates the FK + IK control rig
```

The parser and model layers do not import `bpy`, so the heavy logic can be
tested with a plain Python interpreter.

## Contributing

Bug reports with a sample `.ma` that reproduces the problem are the most useful
thing you can send. See [CONTRIBUTING.md](CONTRIBUTING.md) for how the code is
laid out and how to run the checks.

## License

GPL-3.0-or-later. Blender add-ons link against `bpy` and have to be GPL
compatible.
