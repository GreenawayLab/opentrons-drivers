"""
ssh_handler.py

Pure blocking SSH/SCP transport for Opentrons control.
It is designed to be called from an asyncio event loop
via run_in_executor().
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence, Optional


class SSHError(RuntimeError):
    """Raised when an SSH or SCP command fails."""


class SSHClient:
    """
    Low-level SSH / SCP client.

    Responsibilities:
    - run remote commands
    - upload files
    - download files

    This class is BLOCKING and STATELESS.
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
            "-O",  # force legacy scp protocol (bcs f*** you that's why)
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
        A conceptual copy of korobka's ot_ssh.

        remote_cmd is executed via `sh -lc` to allow chaining.
        """
        cmd = (
            self._base_ssh_cmd()
            + [f"{self.user}@{self.host}", "sh", "-lc", remote_cmd]
        )
        self._run(cmd, timeout=timeout)

    def upload(
        self,
        local: Path,
        remote: str,
    ) -> None:
        """
        Upload a file to the remote host.
        """
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
        """
        Download a file from the remote host.
        """
        local = local.resolve()
        local.parent.mkdir(parents=True, exist_ok=True)
        cmd = (
            self._base_scp_cmd()
            + [f"{self.user}@{self.host}:{remote}", str(local)]
        )
        self._run(cmd)


class OTRuntime:
    """
    Thin Opentrons-specific wrapper around SSHClient.

    Responsibilities:
    - create job workdir
    - start agent via nohup
    - upload postbox files

    """

    def __init__(
        self,
        host: str,
        user: str,
        key_path: Path,
    ):
        self.ssh = SSHClient(host=host, user=user, key_path=key_path)  
        self.workdir = '/'

    # ------------------------------------------------------------------

    def prepare_dir(self, protocol_name: str) -> None:
        """
        Ensure protocol directory structure exists on OT.

        Layout:
        <workdir>/<protocol_name>/
            postbox/
            configs/
            logs/
        """
        base = f"{self.workdir}/{protocol_name}"
        cmd = (
            f"mkdir -p "
            f"{base}/postbox "
            f"{base}/configs "
            f"{base}/logs"
        )
        self.ssh.run(cmd)

    # ------------------------------------------------------------------

    def start_agent(self) -> None:
        """
        Start agent in detached mode using nohup.

        Assumptions:
        - OT has nohup
        - opentrons_execute is in PATH
        """
        cmd = (
            f"cd {self.workdir} && "
            "nohup opentrons_execute "
            "-m opentrons_drivers.agent.agent_main "
            "> agent.log 2>&1 < /dev/null &"
        )
        self.ssh.run(cmd)

    # ------------------------------------------------------------------

    def upload_postbox_file(self, protocol_name: str, local: Path) -> None:
        """
        Major engine of communication with the agent.
        """
        remote = f"{self.workdir}/{protocol_name}/postbox/{local.name}"
        self.ssh.upload(local, remote)


    # ------------------------------------------------------------------

    def read_status(self, local_tmp: Path) -> None:
        """
        Download status.json from the postbox to see how the action goes.
        """
        self.ssh.download(
            f"{self.workdir}/postbox/status.json",
            local_tmp,
        )

    def send_stop(self, protocol_name: str) -> None:
        """
        Send a stop instruction to the agent -> shutdown.
        """
        remote = f"/{protocol_name}/postbox/stop.json"
        self.ssh.run(f"touch {remote}")
