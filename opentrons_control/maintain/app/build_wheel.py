#!/usr/bin/env python3
"""
Build a wheel for the current opentrons_drivers library and store it
in a designated output directory (`dist/wheels` by default).

This script:
1. Validates that pyproject.toml exists
2. Installs the minimal build backend
3. Builds the wheel
4. Moves the wheel into the target directory
"""

import subprocess
import shutil
from pathlib import Path
import sys


def main():
    repo_root = Path(__file__).resolve().parent
    pyproject = repo_root / "pyproject.toml"

    if not pyproject.exists():
        print("ERROR: pyproject.toml not found. Cannot build wheel.", file=sys.stderr)
        sys.exit(1)

    # Where to store wheels
    wheels_dir = repo_root / "dist" / "wheels"
    wheels_dir.mkdir(parents=True, exist_ok=True)

    print("\n=== Building wheel ===")
    print(f"Project root: {repo_root}")
    print(f"Output dir:    {wheels_dir}\n")

    # Ensure build backend is installed
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "build"], check=True)

    # Build wheel into a temporary "dist" folder
    subprocess.run([sys.executable, "-m", "build", "--wheel"], check=True)

    # Move wheel(s) to target folder
    built_dist = repo_root / "dist"
    for wheel in built_dist.glob("*.whl"):
        print(f"Moving wheel: {wheel.name}")
        shutil.move(str(wheel), str(wheels_dir / wheel.name))

    print("\n=== DONE ===")
    print(f"Wheels are now stored in: {wheels_dir}\n")


if __name__ == "__main__":
    main()
