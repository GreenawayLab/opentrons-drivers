import json
import tempfile
import asyncio
from pathlib import Path
from typing import Any, Dict, Callable
from ssh_handler import OTRuntime


class JobRunner:
    """
    Executes protocol steps sequentially on a single Opentrons robot.

    JobRunner is intentionally agnostic to *where* steps come from.
    It supports both:
    - manual workflows (steps supplied internally by JobManager)
    - auto workflows (steps supplied externally via API calls)

    Responsibilities
    ----------------
    - prepare protocol working directories on the robot
    - launch the remote agent process
    - upload step payloads via SCP
    - poll agent status until completion
    - handle abort requests

    JobRunner does not manage job state persistence or robot locking.
    Those concerns are handled by JobManager.
    """

    def __init__(
        self,
        *,
        ot_runtime: OTRuntime,
        poll_interval: float = 1.5,
    ):
        """
        Parameters
        ----------
        ot_runtime :
            Blocking OT runtime abstraction responsible for SSH/SCP operations.
            All interactions with the robot are delegated to this object.
        poll_interval :
            Interval (in seconds) between status polls during step execution.
        """
        self.ot = ot_runtime
        self.poll_interval = poll_interval

        self._loop = asyncio.get_running_loop()
        self._abort_event = asyncio.Event()
        self._running = False

    # --------------------------------------------------------
    # Public control API
    # --------------------------------------------------------

    def abort(self) -> None:
        """
        Request abortion of the currently running job.

        This method is non-blocking and thread-safe.
        It signals an asyncio.Event which is checked:
        - before step execution
        - during status polling

        The actual stop command is sent lazily when the runner
        reaches a safe interruption point.
        """
        self._abort_event.set()

    # --------------------------------------------------------
    # Async ↔ blocking bridge
    # --------------------------------------------------------

    async def _run_blocking(self, fn: Callable[...], *args):
        """
        Run blocking OT / SSH operation in executor. 
        Main engine for runner - all OT calls go through here.

        Delegates the blocking function to the separate thread 
        from the asyncio Event Loop pool, so the main process is never blocked.

        This way the rest of the code can remain async 
        and the results of the blocking call can be awaited in the main process.
        """
        return await self._loop.run_in_executor(None, fn, *args)

    # --------------------------------------------------------=
    # Lifecycle
    # --------------------------------------------------------

    async def start(self, protocol_name: str) -> None:
        """
        Prepare the remote working directory and launch the OT agent.

        This method:
        - creates protocol-specific folders on the robot
        - starts the agent process via opentrons_execute

        It must be called exactly once before executing any steps.
        """

        if self._running:
            raise RuntimeError("JobRunner already started")

        self.protocol_name = protocol_name

        await self._run_blocking(
            self.ot.prepare_dir,
            protocol_name,
        )
        await self._run_blocking(self.ot.start_agent)

        await asyncio.sleep(120) # shouldn't be like this but ok for very start

        # TODO: "I will add an explicit mechanism of confirming that the hardware is running. 
        # Unfortunately since we have to use the opentrons_execute wrapper that sets
        # the actual motors and stuff, we can not rely on just AGENT being up - we need to 
        # run a mechanical action that should be successfully reported back via status.json
        # so it would be just like move a gantry to a center and back or something simple like that.
        # Upon receiving that special status we can be sure that the OT is ready to accept commands
        # and this start function can return. At the moment it just assumes that 
        # after starting the agent and sleeping for a bit (typical hardware wakeup is like ~60-80s)"

        self._running = True

    # --------------------------------------------------------
    # Step execution
    # --------------------------------------------------------

    async def execute_step(self, payload: Dict[str, Any]) -> None:
        """
        Execute a single step payload. 
        This uses temp files to match the OT SCP-based communication.
        Execution completion is detected by polling the agent's status file.

        Parameters
        ----------
        payload :
            Dict to be serialized and sent to the OT agent. 
            Basically expected content of action. 
        """
        if not self._running:
            raise RuntimeError("JobRunner not started")

        if self._abort_event.is_set():
            await self._handle_abort()
            raise RuntimeError("Job aborted before step execution")

        # ------------------------------------------------------------------
        # Materialise payload as temp JSON file
        # ------------------------------------------------------------------

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
        ) as f:
            json.dump(payload, f, indent=2)
            tmp_path = Path(f.name)

        try:
            # Send instruction to OT
            await self._run_blocking(
                self.ot.upload_postbox_file,
                tmp_path,
            )

            # Wait for agent to complete step
            await self._wait_for_completion()

        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass

    # --------------------------------------------------------
    # Polling / status handling
    # --------------------------------------------------------

    async def _wait_for_completion(self) -> None:
        """
        Poll the agent status file until the step completes or fails.

        This method:
        - periodically fetches status.json from the robot
        - yields control to the event loop between polls
        - reacts immediately to abort requests
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "status.json"

            while True:
                # Abort check (immediate)
                if self._abort_event.is_set():
                    await self._handle_abort()
                    raise RuntimeError("Job aborted during step execution")

                try:
                    # Fetch status.json from OT
                    await self._run_blocking(
                        self.ot.read_status,
                        self.protocol_name,
                        status_path,
                    )

                    with open(status_path, "r") as f:
                        payload = json.load(f)

                    status = payload.get("status")
                    error = payload.get("error")

                    if error is not None:
                        raise RuntimeError(
                            f"OT step failed:\n{error}"
                        )

                    if status == "completed":
                        return

                except FileNotFoundError:
                    # status.json not written yet → normal
                    pass

                except json.JSONDecodeError:
                    # partially written file → retry
                    pass

                await asyncio.sleep(self.poll_interval)

    # --------------------------------------------------------
    # Abort handling
    # --------------------------------------------------------

    async def _handle_abort(self) -> None:
        """
        Send stop command to OT agent.
        """
        try:
            await self._run_blocking(self.ot.send_stop)
        finally:
            self._running = False
