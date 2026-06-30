#!/usr/bin/env python3
"""
Bring up the control-plane stack: generate local secrets, start the containers,
and ensure an admin account exists.

Cross-platform — it shells out to the ``docker compose`` CLI and runs
``setup_env.py`` with the same Python interpreter, so it needs no bash. Run it
directly or, after ``pip install -e .``, via the ``letsgo`` command. Identical
on the lab Pi and a Windows home machine:

    letsgo --build           # (editable install)
    python scripts/launch.py --build

Safe to run repeatedly. ``--reset`` destroys the database volume and starts
fresh (keeps .env, so the Fernet vault key is preserved).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def find_compose_dir() -> Path:
    """Locate the directory holding docker-compose.yml.

    Prefers the directory next to this script (correct for an editable install
    or an in-repo run); otherwise walks up from the current directory. Exits
    with a clear message if it cannot be found — which is what a non-editable
    install run from outside the repo looks like.
    """
    candidates: list[Path] = [HERE.parent, Path.cwd().resolve(), *Path.cwd().resolve().parents]
    seen: set[Path] = set()
    for d in candidates:
        if d in seen:
            continue
        seen.add(d)
        if (d / "docker-compose.yml").exists():
            return d
    print(
        "ERROR: docker-compose.yml not found. Run from inside the repo, or "
        "`pip install -e .` so `letsgo` can locate it.",
        file=sys.stderr,
    )
    sys.exit(1)


def run(cmd: list[str], cwd: Path) -> None:
    """Run ``cmd`` in ``cwd``, streaming output; abort on failure."""
    print("+", " ".join(cmd))
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        sys.exit(result.returncode)


def github_repo_set(env_path: Path) -> bool:
    """True if .env has a non-empty GITHUB_REPO."""
    if not env_path.exists():
        return False
    for line in env_path.read_text().splitlines():
        if line.startswith("GITHUB_REPO=") and line.split("=", 1)[1].strip():
            return True
    return False


def host_ip() -> str | None:
    """Best-effort: the host's IP on its default route (where a remote browser
    would reach the proxy). Sends no packets — the UDP connect just selects the
    source address. Returns None on an air-gapped host with no default route.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the control-plane stack.")
    parser.add_argument("--build", action="store_true", help="rebuild images before starting")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="destroy the database volume and start fresh (keeps .env)",
    )
    args = parser.parse_args()

    root = find_compose_dir()
    env_path = root / ".env"

    if args.reset:
        ans = input(
            "Delete ALL database data (robots, secrets, users)? Type 'wipe' to confirm: "
        )
        if ans != "wipe":
            print("Aborted.")
            sys.exit(1)
        run(["docker", "compose", "down", "-v"], cwd=root)

    # Host-side, before compose reads .env. Idempotent: never overwrites an
    # existing value (notably the Fernet vault key).
    print("Ensuring .env secrets...")
    run([sys.executable, str(HERE / "setup_env.py")], cwd=root)

    if not github_repo_set(env_path):
        print(f"ERROR: set GITHUB_REPO=owner/repo in {env_path}, then re-run.", file=sys.stderr)
        sys.exit(1)

    up = ["docker", "compose", "up", "-d", "--wait"]
    if args.build:
        up.append("--build")
    print("Starting stack...")
    run(up, cwd=root)

    print("Ensuring an admin account exists...")
    run(
        [
            "docker", "compose", "exec", "backend",
            "python", "-m", "opentrons_control.backend.app.scripts.seed_admin",
        ],
        cwd=root,
    )

    ip = host_ip()
    print("Ready. Proxy listening on port 8080:")
    print("  from this machine:     http://localhost:8080")
    if ip:
        print(f"  from another machine:  http://{ip}:8080")
    else:
        print("  from another machine:  http://<this-host-ip>:8080  (e.g. `hostname -I`)")


if __name__ == "__main__":
    main()