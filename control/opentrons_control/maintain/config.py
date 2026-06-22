"""Loading and validation of the robot-deployment configuration.

The operator-facing config lives in ``deploy.toml`` (next to this module by
default). It is intentionally separate from ``backend.json`` (the runtime
robot registry) so that operational deploy settings can change without
touching control-plane config — but it *points at* ``backend.json`` so the
fleet is defined in exactly one place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(RuntimeError):
    """Raised when deploy config or backend config is missing/invalid."""


@dataclass(frozen=True)
class Robot:
    """One robot to deploy to, resolved from ``backend.json``."""

    robot_id: str
    host: str
    user: str
    key_path: Path
    agent_port: int = 9000


@dataclass(frozen=True)
class DeployConfig:
    """Fully-resolved deployment configuration.

    All relative paths from ``deploy.toml`` are resolved against the
    directory containing that file, so the config reads naturally
    regardless of the operator's current working directory.
    """

    expected_version: str | None
    drivers_project_dir: Path
    backend_config_path: Path
    ssh_key_dir: Path
    site_packages: str
    staging_dir: str
    robot_python: str
    use_sudo: bool

    def load_robots(self) -> list[Robot]:
        """Read the robot fleet from the referenced ``backend.json``."""
        if not self.backend_config_path.exists():
            raise ConfigError(
                f"backend config not found: {self.backend_config_path}"
            )
        data = json.loads(self.backend_config_path.read_text())
        robots_raw = data.get("robots")
        if not robots_raw:
            raise ConfigError(
                f"no 'robots' section in {self.backend_config_path}"
            )

        robots: list[Robot] = []
        for robot_id, entry in robots_raw.items():
            try:
                key_path = self.ssh_key_dir / entry["key_name"]
                robots.append(
                    Robot(
                        robot_id=robot_id,
                        host=entry["host"],
                        user=entry["user"],
                        key_path=key_path,
                        agent_port=int(entry.get("agent_port", 9000)),
                    )
                )
            except KeyError as exc:
                raise ConfigError(
                    f"robot {robot_id!r} in backend config is missing {exc}"
                ) from exc
        return robots


def default_config_path() -> Path:
    """Path to the operator-local ``deploy.toml`` alongside this module.

    This file is gitignored; create it once by copying
    ``deploy.example.toml``. See :func:`load_deploy_config` for the error
    raised when it is missing.
    """
    return Path(__file__).resolve().parent / "deploy.toml"


def _resolve(base_dir: Path, value: str) -> Path:
    """Resolve ``value`` relative to ``base_dir`` unless it is absolute."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base_dir / p).resolve()


def load_deploy_config(config_path: Path | None = None) -> DeployConfig:
    """Parse ``deploy.toml`` into a :class:`DeployConfig`.

    Args:
        config_path: Explicit path to the TOML config. Defaults to the
            ``deploy.toml`` next to this module.

    Raises:
        ConfigError: if the file is missing or required keys are absent.
    """
    config_path = (config_path or default_config_path()).resolve()
    if not config_path.exists():
        example = config_path.parent / "deploy.example.toml"
        hint = (
            f"\n  Create it from the template: cp {example} {config_path}"
            if example.exists()
            else ""
        )
        raise ConfigError(f"deploy config not found: {config_path}{hint}")

    base = config_path.parent
    raw = tomllib.loads(config_path.read_text())

    drivers = raw.get("drivers", {})
    robots = raw.get("robots", {})
    install = raw.get("install", {})

    try:
        return DeployConfig(
            expected_version=drivers.get("expected_version"),
            drivers_project_dir=_resolve(
                base, drivers.get("project_dir", "../../../drivers")
            ),
            backend_config_path=_resolve(
                base, robots.get("backend_config", "/data/backend.json")
            ),
            ssh_key_dir=_resolve(base, robots.get("ssh_key_dir", "/data/keys")),
            site_packages=install.get(
                "site_packages",
                "/var/user-packages/usr/lib/python3.12/site-packages",
            ),
            staging_dir=install.get("staging_dir", "/data/driver_updates"),
            robot_python=install.get("python", "python3"),
            use_sudo=bool(install.get("use_sudo", False)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid deploy config {config_path}: {exc}") from exc
