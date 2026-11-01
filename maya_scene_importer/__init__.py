# SPDX-License-Identifier: GPL-3.0-or-later
"""
Maya Scene IO (.ma / .mb)
=========================

Import Maya ASCII (.ma) and Maya Binary (.mb) scenes straight into Blender, no
Maya and no FBX/USD round-trip (or let a local Maya read anything
non-standard), reconstructing as much as the two applications' data models
share:

* polygon meshes (vertices, polygons, all UV sets, hard/soft edges, tweaks)
* the complete joint skeleton at its exact Maya bind pose
* skin-cluster weights → vertex groups + an Armature modifier
* blend-shape targets → shape keys
* materials → Principled BSDF, auto-wired to the matching texture set
* Maya object visibility, organised into tidy collections
* (experimental) control-curve shapes and IK handles

See README.md for exactly what does and does not transfer between Maya and
Blender.
"""

from .bridge_ui import register, unregister  # noqa: F401
