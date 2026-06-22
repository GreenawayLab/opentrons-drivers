#!/usr/bin/env python3
"""Roll the ``opentrons_drivers`` wheel out to the whole robot fleet.

Operator workflow
-----------------
1. Bump ``version`` in ``drivers/pyproject.toml`` (and the matching
   ``expected_version`` guard in ``deploy.toml``).
2. Run ``ot-update-robots`` on the control machine.

What it does
------------
- Builds the drivers wheel from the ``drivers/`` project.
- Verifies the built version matches ``expected_version`` (a guard against
  shipping a version you did not intend). Skip with ``--no-version-check``.
- For every robot in ``backend.json``: copies the wheel over SCP and unpacks
  it into the on-robot site-packages overlay.

Why unpack instead of ``pip install``
--------------------------------------
The robots run a network-isolated, non-standard Opentrons Linux image with
no usable package index and an unreliable ``pip``. A wheel is just a zip
archive, so the most robust install that needs nothing but the stdlib is to
delete the old package tree and ``zipfile.extractall`` the new wheel into
site-packages. ``opentrons`` (the only dependency) is already present in the
system image, so no dependency resolution is required.

The SSH/SCP transport is reused from the bootstrap module so there is a
single SSH implementation in the codebase.
"""

from __future__ import annotations

import argparse
import shlex
import sys
import tempfile
from pathlib import Path

from opentrons_control.backend.app.bootstrap import SSHClient, SSHError
from opentrons_control.maintain.build_wheel import (
    WheelBuildError,
    build_drivers_wheel,
)
from opentrons_control.maintain.config import (
    ConfigError,
    DeployConfig,
    Robot,
    default_config_path,
    load_deploy_config,
)

PACKAGE = "opentrons_drivers"


def _sudo(cfg: DeployConfig) -> str:
    """Optional ``sudo`` prefix for remote commands that touch the overlay."""
    return "sudo " if cfg.use_sudo else ""


def read_installed_version(ssh: SSHClient, cfg: DeployConfig) -> str | None:
    """Return the driver version currently installed on a robot, or None.

    Best-effort: reads the ``.dist-info`` directory name in the overlay
    site-packages. Returns None if nothing is installed or the read fails,
    in which case the caller proceeds with the install anyway.
    """
    # Match opentrons_drivers-<version>.dist-info and print just <version>.
    cmd = (
        f"ls -d {shlex.quote(cfg.site_packages)}/{PACKAGE}-*.dist-info "
        f"2>/dev/null | head -n1"
    )
    try:
        out = ssh.run_output(cmd, timeout=30)
    except SSHError:
        return None
    if not out:
        return None
    name = Path(out).name  # opentrons_drivers-0.1.0.dist-info
    stem = name[: -len(".dist-info")] if name.endswith(".dist-info") else name
    _, _, version = stem.partition("-")
    return version or None


def install_on_robot(
    robot: Robot,
    wheel: Path,
    target_version: str,
    cfg: DeployConfig,
    *,
    force: bool,
) -> str:
    """Install ``wheel`` on a single robot. Returns a short status string.

    Steps, all over the existing SSH transport:
    1. Read the currently-installed version; skip if it already matches
       (unless ``force``).
    2. SCP the wheel into the staging directory.
    3. Remove the old package tree + dist-info from site-packages.
    4. Unpack the wheel into site-packages with the stdlib ``zipfile``.
    5. Verify the freshly-installed version matches the target.
    """
    ssh = SSHClient(host=robot.host, user=robot.user, key_path=robot.key_path)
    sudo = _sudo(cfg)

    current = read_installed_version(ssh, cfg)
    if current == target_version and not force:
        return f"already at {target_version}, skipped"

    # 2. Stage the wheel on the robot.
    remote_wheel = f"{cfg.staging_dir}/{wheel.name}"
    ssh.run(f"mkdir -p {shlex.quote(cfg.staging_dir)}", timeout=60)
    ssh.upload(wheel, remote_wheel)

    # 3. Clear any previous install so no stale files survive an upgrade.
    sp = shlex.quote(cfg.site_packages)
    ssh.run(
        f"{sudo}rm -rf {sp}/{PACKAGE} {sp}/{PACKAGE}-*.dist-info",
        timeout=120,
    )

    # 4. Unpack the wheel (a zip) straight into site-packages.
    py = cfg.robot_python
    extract = (
        f"{sudo}{py} -c "
        f"\"import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])\" "
        f"{shlex.quote(remote_wheel)} {sp}"
    )
    ssh.run(extract, timeout=300)

    # 5. Verify.
    installed = read_installed_version(ssh, cfg)
    if installed != target_version:
        raise SSHError(
            f"post-install version mismatch: expected {target_version}, "
            f"found {installed!r}"
        )
    if current == target_version:
        return f"reinstalled {target_version}"
    return f"updated {current or 'none'} -> {target_version}"


def update_fleet(
    cfg: DeployConfig,
    *,
    version_check: bool,
    force: bool,
    only: list[str] | None,
    dry_run: bool,
) -> int:
    """Build the wheel and install it across the fleet. Returns an exit code."""
    robots = cfg.load_robots()
    if only:
        robots = [r for r in robots if r.robot_id in set(only)]
        if not robots:
            print(f"ERROR: none of {only} found in backend config", file=sys.stderr)
            return 1

    with tempfile.TemporaryDirectory(prefix="ot-wheels-") as tmp:
        wheel, version = build_drivers_wheel(cfg.drivers_project_dir, Path(tmp))

        if version_check and cfg.expected_version is not None:
            if version != cfg.expected_version:
                print(
                    f"ERROR: built version {version} != expected_version "
                    f"{cfg.expected_version} in deploy.toml.\n"
                    f"  Bump drivers/pyproject.toml and deploy.toml together, "
                    f"or pass --no-version-check.",
                    file=sys.stderr,
                )
                return 1

        print(f"\n=== Deploying {PACKAGE} {version} to {len(robots)} robot(s) ===")
        if dry_run:
            for robot in robots:
                print(f"  [dry-run] would update {robot.robot_id} ({robot.host})")
            return 0

        failures = 0
        for robot in robots:
            label = f"{robot.robot_id} ({robot.host})"
            try:
                status = install_on_robot(
                    robot, wheel, version, cfg, force=force
                )
                print(f"  OK   {label}: {status}")
            except (SSHError, OSError) as exc:
                failures += 1
                print(f"  FAIL {label}: {exc}", file=sys.stderr)

    if failures:
        print(f"\n{failures} robot(s) failed.", file=sys.stderr)
        return 1
    print("\nAll robots updated.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point for ``ot-update-robots``."""
    parser = argparse.ArgumentParser(
        description="Build and deploy the opentrons_drivers wheel to the fleet."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="Path to deploy.toml (default: shipped alongside the package).",
    )
    parser.add_argument(
        "--robot",
        action="append",
        dest="only",
        metavar="ROBOT_ID",
        help="Limit to specific robot id(s); repeatable. Default: all robots.",
    )
    parser.add_argument(
        "--no-version-check",
        action="store_false",
        dest="version_check",
        help="Skip the built-version vs expected_version guard.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reinstall even if the robot already reports the target version.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and report what would happen without touching robots.",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_deploy_config(args.config)
        return update_fleet(
            cfg,
            version_check=args.version_check,
            force=args.force,
            only=args.only,
            dry_run=args.dry_run,
        )
    except (ConfigError, WheelBuildError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
