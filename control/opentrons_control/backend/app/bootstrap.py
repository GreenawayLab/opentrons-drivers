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
import opentrons_control.backend.app.settings.global_variables as gv

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
    ) -> subprocess.CompletedProcess[str]:
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

    def run_output(
        self,
        remote_cmd: str,
        *,
        timeout: Optional[int] = None,
    ) -> str:
        """Run a remote shell command via SSH and return its stdout.

        Like :meth:`run`, but returns the command's captured stdout
        (stripped). Used by maintenance tooling that needs to read a value
        back from the robot, e.g. the currently-installed driver version.
        """
        cmd = self._base_ssh_cmd() + [f"{self.user}@{self.host}", remote_cmd]
        return self._run(cmd, timeout=timeout).stdout.strip()

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
        return f"{gv.OT_WORKDIR}/{self.protocol_name}/{self.launch_id}"

    def subdir(self, name: str) -> str:
        """Absolute path of a named subdirectory inside the launch directory."""
        return f"{self.launch_dir}/{name}"

    # ------------------------------------------------------------------

    def prepare_dir(self) -> None:
        """Create the launch directory tree on the OT."""
        paths = " ".join(self.subdir(name) for name in gv.LAUNCH_SUBDIRS)
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

        Steps, all in one remote shell command:

        - Stops ``opentrons-robot-server`` if it's running. The systemd
          service grabs exclusive hardware locks (GPIO, smoothie serial)
          on boot; ``opentrons_execute`` cannot acquire them while the
          server is alive. ``|| true`` swallows the failure when the
          service is already stopped.
        - Resolves ``agent_main.py`` at runtime as a
          Python-version-specific site-packages path. ``pip show`` reports
          where ``opentrons_drivers`` is installed (its ``Location``), and
          the agent module sits at a fixed relative path beneath it
          (:data:`AGENT_MAIN_RELPATH`). The discovery and a ``test -f``
          guard run in the foreground; only the ``nohup`` launch is
          backgrounded (via the brace group), so a missing or uninstalled
          package fails as an SSHError here rather than as a silent
          non-launch that only shows up as a readiness timeout.
        - Exports :data:`AGENT_ENV` for the agent process. Most importantly
          ``PYTHONUNBUFFERED=1`` so runlog output is visible in
          ``agent.log`` in real time, not after process exit.

        Assumes the OT has ``nohup``, ``opentrons_execute``, the pip given
        by :data:`ROBOT_PIP`, and ``sed`` on PATH, and ``opentrons_drivers``
        installed.
        """
        env_prefix = " ".join(f"{k}={v}" for k, v in gv.AGENT_ENV.items())

        # $(pip show ...) yields the install Location; append the fixed
        # relative path to reach agent_main.py. Word-splitting does not apply
        # to an assignment RHS, so a Location with spaces survives; we quote
        # on every use below.
        location = (
            f"$({gv.ROBOT_PIP} show {gv.DRIVERS_PACKAGE} "
            f"| sed -n 's/^Location: //p')"
        )

        cmd = (
            f"systemctl stop opentrons-robot-server || true; "
            f"cd {self.launch_dir} && "
            f"AGENT_MAIN={location}/{gv.AGENT_MAIN_RELPATH} && "
            f'test -f "$AGENT_MAIN" && '
            f"{{ nohup env {env_prefix} "
            f'opentrons_execute "$AGENT_MAIN" '
            f"> {self.launch_dir}/logs/agent.log 2>&1 < /dev/null & }}"
        )
        self.ssh.run(cmd)