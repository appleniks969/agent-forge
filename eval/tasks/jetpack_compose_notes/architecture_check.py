#!/usr/bin/env python3
"""
architecture_check.py — verify the layering rules for the Compose notes task.

Rules checked:
  1. ViewModel layer must NOT import androidx.compose.{ui,foundation,material*}
     (it should be pure logic, runnable on plain JVM with no UI deps)
  2. Data/repository layer must NOT import viewmodel.* or ui.*
  3. Model layer must NOT import data.*, viewmodel.*, or ui.*
  4. Project must contain >= 5 .kt files under src/main/kotlin
  5. Project must use >= 3 distinct top-level packages under src/main/kotlin

We classify a source file's "layer" by its package declaration (preferred)
or by its path (fallback).

Outputs JSON to stdout.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PACKAGE_RE = re.compile(r"^\s*package\s+([a-zA-Z_][\w.]*)", re.MULTILINE)
IMPORT_RE = re.compile(r"^\s*import\s+([a-zA-Z_][\w.]*)", re.MULTILINE)

# Layer detection — package prefix → layer
LAYER_BY_PREFIX = {
    "ui": "ui",
    "presentation": "ui",
    "viewmodel": "viewmodel",
    "vm": "viewmodel",
    "data": "data",
    "repository": "data",
    "repo": "data",
    "model": "model",
    "domain": "model",
    "entity": "model",
}

# Forbidden imports per layer (substring matches against import target)
FORBIDDEN = {
    "viewmodel": [
        "androidx.compose.ui",
        "androidx.compose.foundation",
        "androidx.compose.material",
    ],
    "data": ["viewmodel.", "ui.", "presentation."],
    "model": ["data.", "repository.", "viewmodel.", "ui.", "presentation."],
}


def classify(file_path: Path, package: str | None) -> str:
    # Check every segment of the package, not just the first — agents often
    # use qualified packages like `com.foo.data` or `eval.notes.viewmodel`.
    if package:
        for segment in package.split("."):
            if segment in LAYER_BY_PREFIX:
                return LAYER_BY_PREFIX[segment]
    parts = file_path.parts
    for part in parts:
        if part in LAYER_BY_PREFIX:
            return LAYER_BY_PREFIX[part]
    return "unknown"


def package_to_layer_segments(packages: set[str]) -> set[str]:
    """Reduce qualified packages to layer prefixes seen anywhere in the FQN."""
    seen: set[str] = set()
    for pkg in packages:
        for seg in pkg.split("."):
            if seg in LAYER_BY_PREFIX:
                seen.add(LAYER_BY_PREFIX[seg])
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True, type=Path)
    args = ap.parse_args()

    src_root = args.workdir / "src" / "main" / "kotlin"
    if not src_root.exists():
        json.dump(
            {"architecture_error": f"missing {src_root}", "passed": False},
            sys.stdout,
            indent=2,
        )
        return 0

    files = sorted(src_root.rglob("*.kt"))
    violations: list[dict] = []
    file_records: list[dict] = []
    packages_seen: set[str] = set()

    for f in files:
        text = f.read_text(errors="replace")
        pkg_match = PACKAGE_RE.search(text)
        package = pkg_match.group(1) if pkg_match else None
        if package:
            packages_seen.add(package.split(".")[0])
        layer = classify(f.relative_to(src_root), package)

        imports = IMPORT_RE.findall(text)
        bad_imports = []
        for forbidden in FORBIDDEN.get(layer, []):
            for imp in imports:
                if forbidden in imp:
                    bad_imports.append({"import": imp, "rule": f"{layer} must not import {forbidden}"})
        if bad_imports:
            violations.append(
                {
                    "file": str(f.relative_to(args.workdir)),
                    "layer": layer,
                    "violations": bad_imports,
                }
            )
        file_records.append(
            {
                "file": str(f.relative_to(src_root)),
                "package": package,
                "layer": layer,
                "import_count": len(imports),
            }
        )

    file_count = len(files)
    # Count distinct LAYERS observed (e.g. data + viewmodel + model + ui = 4)
    # rather than distinct first-segments (a single qualified prefix would
    # otherwise collapse everything to one).
    full_packages = {f.get("package") or "" for f in file_records if f.get("package")}
    layers_seen = package_to_layer_segments(full_packages)
    pkg_count = len(layers_seen)

    rule_results = {
        "rule_min_5_files": file_count >= 5,
        "rule_min_3_packages": pkg_count >= 3,
        "rule_no_forbidden_imports": len(violations) == 0,
    }
    passed = all(rule_results.values())

    json.dump(
        {
            "passed": passed,
            "kt_file_count": file_count,
            "package_count": pkg_count,
            "packages": sorted(packages_seen),
            "layers_seen": sorted(layers_seen),
            "rules": rule_results,
            "violations": violations,
            "files": file_records,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
