"""
Blocking SSH/SCP transport for Opentrons bootstrap.

SSH is used only for work that cannot be done over HTTP because the agent
isn't running yet: creating the launch directory tree, uploading boot-time
files, and launching the agent process via opentrons_execute. Runtime
communication with a live agent (action submission, status polling,
abort) is done over HTTP elsewhere.

These calls are blocking and intended to be invoked from an asyncio event
loop via run_in_executor().
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Sequence, Optional


class SSHError(RuntimeError):
    """Raised when an SSH or SCP command fails."""


class SSHClient:
    """
    Low-level SSH / SCP client.

    Supports running remote commands, uploading files, and downloading
    files. Blocking and stateless: every public method spawns a fresh
    subprocess and returns when it exits.
    """

    def __init__(
        self,
        host: str,
        user: str,
        key_path: Path,
        *,
        port: int = 22,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path

    # ------------------------------------------------------------------
    # Core helpers
    # ------------------------------------------------------------------

    def _base_ssh_cmd(self) -> list[str]:
        return [
            "ssh",
            "-i", str(self.key_path),
            "-p", str(self.port),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
        ]

    def _base_scp_cmd(self) -> list[str]:
        return [
            "scp",
            "-O",  # force legacy scp protocol
            "-i", str(self.key_path),
            "-P", str(self.port),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
        ]

    def _run(
        self,
        cmd: Sequence[str],
        *,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise SSHError(
                f"Command failed:\n"
                f"CMD: {' '.join(cmd)}\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        remote_cmd: str,
        *,
        timeout: Optional[int] = None,
    ) -> None:
        """
        Run a remote shell command via SSH.

        ``remote_cmd`` is passed as a single argument to ssh, which the
        remote side parses with its default shell. Shell features like
        chaining, redirection, and globbing work as written.
        """
        cmd = self._base_ssh_cmd() + [f"{self.user}@{self.host}", remote_cmd]
        self._run(cmd, timeout=timeout)

    def upload(
        self,
        local: Path,
        remote: str,
    ) -> None:
        """Upload a single file to the remote host."""
        local = local.resolve()
        cmd = (
            self._base_scp_cmd()
            + [str(local), f"{self.user}@{self.host}:{remote}"]
        )
        self._run(cmd)

    def download(
        self,
        remote: str,
        local: Path,
    ) -> None:
        """Download a file from the remote host."""
        local = local.resolve()
        local.parent.mkdir(parents=True, exist_ok=True)
        cmd = (
            self._base_scp_cmd()
            + [f"{self.user}@{self.host}:{remote}", str(local)]
        )
        self._run(cmd)


#: Absolute path to agent_main.py on the Opentrons system.
#:
#: The path is firmware-version-dependent: the user-packages overlay layout
#: is an Opentrons system convention, and the python3.12 segment changes
#: with the system Python version. If the agent fails to launch with
#: "no such file", check here first.
AGENT_MAIN_PATH = (
    "/var/user-packages/usr/lib/python3.12/site-packages"
    "/opentrons_drivers/agent/agent_main.py"
)

#: Root directory on the OT where all protocol launches are organised.
OT_WORKDIR = "/data/protocols"

#: Subdirectories created under each launch directory. The agent reads
#: files from these by name relative to its cwd.
LAUNCH_SUBDIRS = ("postbox", "plates", "logs")

#: Environment variables exported into the agent process. RUNNING_ON_PI
#: tells the Opentrons API it's on real hardware (suppresses an emulator
#: warning, mostly cosmetic). PYTHONUNBUFFERED forces stdout/stderr to
#: flush every line — without it, runlog output gets stuck in Python's
#: block buffer and only appears in agent.log on process exit, defeating
#: live diagnostics.
AGENT_ENV = {
    "RUNNING_ON_PI": "1",
    "PYTHONUNBUFFERED": "1",
}


class OTBootstrap:
    """
    Opentrons-specific bootstrap helper.

    A single instance corresponds to one launch attempt on one robot.
    Owns the launch directory layout, file population, and agent process
    start. Exposes no runtime methods; once the agent is running, all
    communication with it goes over HTTP.

    Directory layout on the OT::

        <OT_WORKDIR>/<protocol>/<launch_id>/
            postbox/   # boot-time configuration (base_config.json, etc.)
            plates/    # labware definitions referenced by base_config
            logs/      # agent stdout/stderr

    The launch_id scopes a single run; repeated launches of the same
    protocol do not collide.
    """

    def __init__(
        self,
        host: str,
        user: str,
        key_path: Path,
        protocol_name: str,
        launch_id: str,
    ):
        self.ssh = SSHClient(host=host, user=user, key_path=key_path)
        self.protocol_name = protocol_name
        self.launch_id = launch_id

    # ------------------------------------------------------------------

    @property
    def launch_dir(self) -> str:
        """Absolute path of the per-launch directory on the OT."""
        return f"{OT_WORKDIR}/{self.protocol_name}/{self.launch_id}"

    def subdir(self, name: str) -> str:
        """Absolute path of a named subdirectory inside the launch directory."""
        return f"{self.launch_dir}/{name}"

    # ------------------------------------------------------------------

    def prepare_dir(self) -> None:
        """Create the launch directory tree on the OT."""
        paths = " ".join(self.subdir(name) for name in LAUNCH_SUBDIRS)
        self.ssh.run(f"mkdir -p {paths}")

    # ------------------------------------------------------------------

    def upload_files_to(self, subdir: str, locals_: Iterable[Path]) -> None:
        """
        Upload one or more files into a named subdirectory of the launch
        directory.

        Each file is uploaded under its basename. The caller is
        responsible for ensuring the filenames match what the agent
        expects (e.g. ``base_config.json`` in ``postbox/``,
        ``standard_plate.json`` in ``plates/``).
        """
        target = self.subdir(subdir)
        for local in locals_:
            self.ssh.upload(local, f"{target}/{local.name}")

    # ------------------------------------------------------------------

    def start_agent(self) -> None:
        """
        Launch the agent process detached, with cwd set to the launch
        directory.

        Performs two pre-launch steps:

        - Stops ``opentrons-robot-server`` if it's running. The systemd
          service grabs exclusive hardware locks (GPIO, smoothie serial)
          on boot; ``opentrons_execute`` cannot acquire them while the
          server is alive. ``|| true`` swallows the failure when the
          service is already stopped.
        - Exports :data:`AGENT_ENV` for the agent process. Most
          importantly ``PYTHONUNBUFFERED=1`` so runlog output is visible
          in ``agent.log`` in real time, not after process exit.

        ``opentrons_execute`` is invoked with the absolute path to
        ``agent_main.py``; it is not a ``python -m`` runner.

        Assumes the OT has ``nohup``, ``opentrons_execute`` on PATH, and
        ``opentrons_drivers`` installed at :data:`AGENT_MAIN_PATH`.
        """
        env_prefix = " ".join(f"{k}={v}" for k, v in AGENT_ENV.items())

        cmd = (
            f"systemctl stop opentrons-robot-server || true && "
            f"cd {self.launch_dir} && "
            f"nohup env {env_prefix} "
            f"opentrons_execute {AGENT_MAIN_PATH} "
            f"> {self.launch_dir}/logs/agent.log 2>&1 < /dev/null &"
        )
        self.ssh.run(cmd)