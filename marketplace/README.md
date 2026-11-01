# Maya ASCII Scene IO

Import Maya ASCII (`.ma`) and Maya Binary (`.mb`) scenes in Blender 4.2 or
newer, and export `.ma`. The extension reads both formats in Python and does
not need Maya or another external program. It reads files from old Maya
versions too.

It transfers meshes, UVs, vertex colours, materials, textures, skeletons, skin
weights, blend shapes, cameras, lights, visibility, and animation. It can also
build a basic FK and IK control rig from the imported skeleton.

This edition supports files with standard nodes. Blend shapes, vertex colours
and creases are not read from `.mb` yet, and meshes that exist only as
construction history (an un-baked `polyCube`, for example) are skipped with a
warning; delete history in Maya to bake them.

## Use

- Import: `File > Import > Maya Scene (.ma/.mb)`
- Export: `File > Export > Maya Scene (.ma)`
- Sidebar: press `N` in the 3D Viewport and open the `Maya IO` tab

The Maya control rig's logic, including FK/IK switches, driven keys, and
utility-node graphs, cannot be transferred to Blender. The deform skeleton,
skin weights, and blend shapes do transfer.

## License

GPL-3.0-or-later. Full documentation and source are available in the
[project repository](https://github.com/MenuuTUX/maya-scene-io).
