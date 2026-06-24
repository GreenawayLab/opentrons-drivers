"""
Build the ``opentrons_drivers`` wheel from a checkout of the monorepo.

The maintainer ships only the drivers subpackage as a wheel — never the
control plane. The drivers project has its own ``pyproject.toml``; this
module builds it with ``python -m build`` and reports the resulting wheel and
its version.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PACKAGE = "opentrons_drivers"


class WheelBuildError(RuntimeError):
    """Raised when the wheel cannot be built or located."""


def build_drivers_wheel(project_dir: Path, output_dir: Path) -> tuple[Path, str]:
    """Build the drivers wheel and return ``(wheel_path, version)``.

    :param project_dir: Drivers project root (the dir holding pyproject.toml).
    :param output_dir: Directory the finished wheel is written to; created if
        missing and cleared of any prior drivers wheel first, so it ends with
        exactly one.
    :returns: ``(wheel_path, version)``.
    :raises WheelBuildError: if the project is missing or the build produces
        no wheel.
    """
    project_dir = project_dir.resolve()
    if not (project_dir / "pyproject.toml").exists():
        raise WheelBuildError(f"no pyproject.toml at {project_dir}")

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob(f"{PACKAGE}-*.whl"):
        stale.unlink()

    # `python -m build --wheel --outdir <dir> <project>` builds the package
    # rooted at <project> using its own build-system and drops the wheel into
    # <dir>. No chdir, no global state.
    try:
        subprocess.run(
            [
                sys.executable, "-m", "build", "--wheel",
                "--outdir", str(output_dir), str(project_dir),
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise WheelBuildError(f"build failed: {exc}") from exc

    wheels = sorted(output_dir.glob(f"{PACKAGE}-*.whl"))
    if not wheels:
        raise WheelBuildError(f"build produced no wheel in {output_dir}")
    wheel = wheels[-1]

    # opentrons_drivers-<version>-py3-none-any.whl -> version is field 1.
    parts = wheel.name.split("-")
    if len(parts) < 2:
        raise WheelBuildError(f"cannot parse version from wheel name: {wheel.name}")
    return wheel, parts[1]
