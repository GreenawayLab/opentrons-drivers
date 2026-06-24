#!/usr/bin/env python3
"""Build the ``opentrons_drivers`` wheel for deployment to the robots.

The robots run an esoteric, network-isolated Opentrons Linux image where a
normal ``pip install`` from an index is not possible. The deployment story
is therefore: build a wheel here on the control machine, copy it to each
robot, and unpack it into the on-robot site-packages overlay (see
:mod:`opentrons_control.maintain.update_robots`).

This module only handles the *build* half. It is deliberately importable
(``build_drivers_wheel``) so the updater can reuse it, and also runnable as
a console script (``ot-build-wheel``).

Unlike the control plane, this code is run from a checkout of the monorepo
on the control machine, because it needs the ``drivers/`` source to build.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


class WheelBuildError(RuntimeError):
    """Raised when the wheel cannot be built or located."""


def default_drivers_project_dir() -> Path:
    """Best-effort location of the ``drivers/`` project in the monorepo.

    Walks up from this file looking for a ``drivers/pyproject.toml``. This
    works when running from a normal checkout; if the layout differs, pass
    an explicit path instead.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "drivers"
        if (candidate / "pyproject.toml").exists():
            return candidate
    # Fall back to <repo_root>/drivers relative to this file's known depth
    # (maintain -> opentrons_control -> control -> repo_root).
    return here.parents[3] / "drivers"


def _wheel_version(wheel: Path) -> str:
    """Extract the version from a wheel filename.

    Wheel names look like ``opentrons_drivers-0.1.0-py3-none-any.whl``;
    the version is the second ``-``-separated field.
    """
    parts = wheel.name.split("-")
    if len(parts) < 2:
        raise WheelBuildError(f"Cannot parse version from wheel name: {wheel.name}")
    return parts[1]


def build_drivers_wheel(
    project_dir: Path,
    output_dir: Path,
    *,
    clean: bool = True,
) -> tuple[Path, str]:
    """Build the drivers wheel and return ``(wheel_path, version)``.

    Args:
        project_dir: Path to the ``drivers/`` project root (contains
            ``pyproject.toml``).
        output_dir: Directory the finished wheel is placed in. Created if
            missing.
        clean: When true, remove any pre-existing ``opentrons_drivers``
            wheels from ``output_dir`` first, so the directory ends up with
            exactly one wheel and the updater is never ambiguous about which
            to ship.

    Returns:
        A ``(wheel_path, version)`` tuple.

    Raises:
        WheelBuildError: if the project is missing or the build produces no
            wheel.
    """
    project_dir = project_dir.resolve()
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        raise WheelBuildError(
            f"No pyproject.toml at {pyproject}. Point --project-dir at the "
            f"drivers/ project root."
        )

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if clean:
        for stale in output_dir.glob("opentrons_drivers-*.whl"):
            stale.unlink()

    print("=== Building opentrons_drivers wheel ===")
    print(f"  project: {project_dir}")
    print(f"  output:  {output_dir}")

    # `python -m build --wheel --outdir <dir> <project>` builds the package
    # rooted at <project> using its own pyproject/build-system and drops the
    # wheel straight into <dir>. No global state, no chdir, no shutil.move.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(output_dir),
            str(project_dir),
        ],
        check=True,
    )

    wheels = sorted(output_dir.glob("opentrons_drivers-*.whl"))
    if not wheels:
        raise WheelBuildError(f"Build produced no wheel in {output_dir}")
    wheel = wheels[-1]
    version = _wheel_version(wheel)
    print(f"  built:   {wheel.name} (version {version})")
    return wheel, version


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point for ``ot-build-wheel``."""
    parser = argparse.ArgumentParser(description="Build the opentrons_drivers wheel.")
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=default_drivers_project_dir(),
        help="Path to the drivers/ project root (default: autodetected).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / "dist" / "wheels",
        help="Directory to write the wheel into (default: ./dist/wheels).",
    )
    args = parser.parse_args(argv)

    try:
        build_drivers_wheel(args.project_dir, args.output_dir)
    except (WheelBuildError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
