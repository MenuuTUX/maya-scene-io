#!/usr/bin/env python3
"""Build the native-only package (no Maya bridge) for Blender Extensions."""

import argparse
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# The shared UI (ui.py) and the importers, including the pure-Python .mb reader,
# ship as they are; maya_bridge.py and bridge_ui.py stay out, because Blender
# Extensions only hosts add-ons that do not depend on other applications.  __init__.py comes from marketplace/ and
# only wires up ui.py.
PACKAGE_FILES = (
    "ui.py",
    "ma_parser.py",
    "ma_refs.py",
    "mb_parser.py",
    "ma_model.py",
    "scene_builder.py",
    "ma_exporter.py",
    "rig_builder.py",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", default="blender",
                        help="Blender executable (default: blender)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist",
                        help="Directory for the built ZIP (default: dist)")
    args = parser.parse_args()

    source = ROOT / "maya_scene_importer"
    manifest = (ROOT / "marketplace" / "blender_manifest.toml").read_text(
        encoding="utf-8")
    full_manifest = (source / "blender_manifest.toml").read_text(
        encoding="utf-8")
    version_match = re.search(r'^version = "([^"]+)"$', full_manifest,
                              re.MULTILINE)
    if not version_match:
        raise SystemExit("Could not read add-on version from full manifest")
    manifest = re.sub(r'^version = "[^"]+"$',
                      'version = "' + version_match.group(1) + '"',
                      manifest, count=1, flags=re.MULTILINE)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="maya-ascii-extension-") as tmp:
        package = Path(tmp) / "maya_ascii_scene_io"
        package.mkdir()
        for name in PACKAGE_FILES:
            shutil.copy2(source / name, package / name)
        shutil.copy2(ROOT / "marketplace" / "__init__.py",
                     package / "__init__.py")
        shutil.copy2(ROOT / "LICENSE", package / "LICENSE")
        shutil.copy2(ROOT / "marketplace" / "README.md",
                     package / "README.md")
        (package / "blender_manifest.toml").write_text(
            manifest, encoding="utf-8")
        subprocess.run([
            args.blender,
            "--command", "extension", "build",
            "--source-dir", str(package),
            "--output-dir", str(args.output_dir.resolve()),
        ], check=True)


if __name__ == "__main__":
    main()
