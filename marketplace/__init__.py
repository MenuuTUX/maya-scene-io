# SPDX-License-Identifier: GPL-3.0-or-later
"""
Maya ASCII Scene IO (.ma)
=========================

Import Maya ASCII (.ma) and Maya Binary (.mb) scenes straight into Blender,
with no other application involved, reconstructing as much as the two
applications' data models share:

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

from .ui import register, unregister  # noqa: F401
