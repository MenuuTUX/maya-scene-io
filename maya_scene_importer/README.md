# Maya Scene IO

Import and export Maya scenes (`.ma` / `.mb`) in Blender.

## Two engines

- **Native** reads `.ma` directly, no Maya needed. Handles meshes, UVs, the
  skeleton, skin weights, blend shapes, and lambert/blinn/phong materials.
- **Maya bridge** drives a local `mayapy` to convert any `.ma` or `.mb` to FBX,
  then imports that. Needed for `.mb` and for files with plugin nodes. Set the
  Maya path in `Edit > Preferences > Add-ons > Maya Scene IO`.

`Auto` uses Native for `.ma` and the bridge for `.mb`.

## Usage

- Import: `File > Import > Maya Scene`
- Export: `File > Export > Maya Scene`
- Sidebar: press `N` in the 3D viewport and open the "Maya IO" tab

## Note on rigs

The deform skeleton, skin weights, and blend shapes transfer. The Maya control
rig's logic (FK/IK blends, set-driven keys, the node graph) cannot be reproduced
in Blender and does not transfer. The add-on can generate a basic FK + IK rig
from the file to get you posing right away.

Full documentation and source: see the project repository. Licensed under
GPL-3.0-or-later.
