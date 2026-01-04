from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Literal
from dataclasses import dataclass, field
from opentrons_control.backend.app.ssh_handler import OTRuntime
from dataclasses import dataclass

Mode = Literal["manual", 
               "auto"]

JobStatus = Literal[
            "created",
            "starting",
            "running",
            "waiting",
            "completed",
            "aborted",
            "failed"]

@dataclass
class Robot:
    """
    Static description of a robot accessible by the backend.
    Robot addition and management will be automated later as well.

    This object does not represent runtime state, only connection metadata.
    Runtime exclusivity is enforced separately via asyncio locks.

    Attributes
    ----------
    id :
        Logical robot identifier used by API clients.
    host :
        Network address of the robot.
    user :
        SSH user (typically 'root' for OT systems).
    key_name :
        Filename of the SSH private key, resolved relative to /data/access.
    
    """
    id: str
    host: str
    user: str
    key_name: str   # filename only, resolved via /data/access

@dataclass
class JobState:
    """
    Authoritative, mutable state of a single job execution.

    This structure represents the *current* state only.
    Historical events and step-level logs are written to job-specific
    files on disk and are not retained in memory.

    JobState is rewritten on each state transition and can be:
    - accessed via API
    - persisted to file
    - stored in DB upon job completion

    Attributes
    ----------
    job_id :
        Unique identifier of the job.
    robot_id :
        Robot assigned to this job.
    protocol_name :
        Name of the protocol directory on the robot.
    mode :
        'manual' or 'auto'.
    status :
        High-level lifecycle status of the job.
    current_step :
        Index of the currently executing step (1-based).
    message :
        Human-readable status message.
    updated_at :
        Unix timestamp of last state update. 
    """
    job_id: str
    robot_id: str
    protocol_name: str
    mode: Mode
    status: JobStatus = "created"
    current_step: Optional[int] = None
    message: Optional[str] = None
    updated_at: float = time.time()

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

    async def _run_blocking(self, fn, *args):
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

        time.sleep(120) # shouldn't be like this but ok for very start

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
                self.protocol_name,
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


class JobManager:
    """
    Central coordinator for backend job execution.

    JobManager is responsible for:
    - job registration and lifecycle management
    - enforcing robot exclusivity
    - creating and owning JobRunner instances
    - exposing job state to the API layer

    Two execution modes are supported:
    - manual: steps defined in instruction.yaml and executed internally 
    - auto: steps are supplied externally and incrementally via API calls

    JobManager itself does not execute hardware actions directly.
    All robot interaction is delegated to JobRunner instances.
    """
    def __init__(
        self,
        *,
        robots: Dict[str, Robot],
        access_dir: Path = Path("/data/access"),
        poll_interval: float = 1.5,
    ):
        """
        Triggered when backend is started.
        
        Parameters
        ----------
        robots: Dict[str, Robot]
            Mapping of robot_id to Robot instances. Read from some config in the future.
        access_dir: Path
            Directory where SSH keys are stored.
        poll_interval: float
            Seconds between status polls to OT.
        """
        self.robots = robots
        self.access_dir = access_dir
        self.poll_interval = poll_interval

        self._robot_locks: Dict[str, asyncio.Lock] = {
            rid: asyncio.Lock() for rid in robots
        }

        self._states: Dict[str, JobState] = {}
        self._jobs: Dict[str, JobRunner] = {}

        # Protect job registration
        self._manager_lock = asyncio.Lock()

    # --------------------------------------------------------
    # Public state access
    # --------------------------------------------------------

    def get_state(self, job_id: str) -> JobState:
        if job_id not in self._states:
            raise KeyError(f"Unknown job: {job_id}")
        return self._states[job_id]

    # --------------------------------------------------------
    # Entry point
    # --------------------------------------------------------

    async def submit_job(
        self,
        *,
        job_id: str,
        robot_id: str,
        extract_dir: Path,
    ) -> JobState:
        """
        Submit a job from an extracted archive, containing config files for agent.
        Manual if instruction.yaml exists, otherwise auto.

        This method:
        - registers the job
        - acquires exclusive access to the target robot
        - starts the remote agent
        - returns immediately once the job is initialized

        Parameters
        ----------
        job_id : str (for now, the name of the protocol, later some unique stuff)
            Unique job ID.
        robot_id : str (TrixieMixie and so one - acceptable on the small scale)
            Target robot ID.
        extract_dir : Path
            Path to extracted archive directory.

        Returns
        -------
        JobState :
            Initial state of the created job.
        """
        instruction = extract_dir / "instruction.yaml"
        mode: Mode = "manual" if instruction.exists() else "auto"
        protocol_name = extract_dir.name

        if mode == "manual":
            return await self._start_manual_job(
                job_id=job_id,
                robot_id=robot_id,
                protocol_name=protocol_name,
                instruction_path=instruction,
                extract_dir=extract_dir,
            )
        else:
            return await self._start_auto_job(
                job_id=job_id,
                robot_id=robot_id,
                protocol_name=protocol_name,
            )

    # --------------------------------------------------------
    # Manual job
    # --------------------------------------------------------

    async def _start_manual_job(
        self,
        *,
        job_id: str,
        robot_id: str,
        protocol_name: str,
        instruction_path: Path,
        extract_dir: Path,
    ) -> JobState:
        """
        Initialize and start a manual job.

        Manual jobs execute all steps sequentially in a background
        asyncio.Task (the manual feeder), allowing the API layer
        to remain responsive.
        """
        async with self._manager_lock:
            if job_id in self._states:
                raise RuntimeError(f"Job already exists: {job_id}")

            self._states[job_id] = JobState(
                job_id=job_id,
                robot_id=robot_id,
                protocol_name=protocol_name,
                mode="manual",
                status="starting",
            )
            self._persist_state(job_id)

        lock = self._get_robot_lock(robot_id)
        await lock.acquire()

        try:
            runner = await self._create_runner(robot_id, protocol_name)
            self._jobs[job_id] = runner

            steps = self._parse_instruction_yaml(instruction_path, extract_dir)

            self._update_state(
                job_id,
                status="running",
                message=f"Manual job started ({len(steps)} steps)",
            )

            asyncio.create_task(
                self._manual_feeder(job_id, runner, steps),
                name=f"manual_feeder:{job_id}",
            )

            return self._states[job_id]

        except Exception:
            lock.release()
            self._update_state(job_id, status="failed", message="Failed to start manual job")
            raise

    # --------------------------------------------------------
    # Auto job
    # --------------------------------------------------------

    async def _start_auto_job(
        self,
        *,
        job_id: str,
        robot_id: str,
        protocol_name: str,
    ) -> JobState:
        """
        Initialize an auto job.

        Auto jobs do not spawn a background execution task.
        Each step is executed synchronously within the API request
        coroutine that calls step_and_wait().
        """
        async with self._manager_lock:
            if job_id in self._states:
                raise RuntimeError(f"Job already exists: {job_id}")

            self._states[job_id] = JobState(
                job_id=job_id,
                robot_id=robot_id,
                protocol_name=protocol_name,
                mode="auto",
                status="starting",
            )
            self._persist_state(job_id)

        lock = self._get_robot_lock(robot_id)
        await lock.acquire()

        try:
            runner = await self._create_runner(robot_id, protocol_name)
            self._jobs[job_id] = runner

            self._update_state(
                job_id,
                status="waiting",
                message="Auto job ready; awaiting steps",
            )
            return self._states[job_id]

        except Exception:
            lock.release()
            self._update_state(job_id, status="failed", message="Failed to start auto job")
            raise

    # --------------------------------------------------------
    # Auto step execution
    # --------------------------------------------------------

    async def step_and_wait(self, job_id: str, payload: Dict[str, Any]) -> None:
        """
        Execute a single step for an auto job and wait for completion.

        This method is intended for programmatic (dark-mode) workflows.
        The calling API request remains suspended until the step finishes,
        providing natural back-pressure and synchronization.
        """
        state = self.get_state(job_id)
        if state.mode != "auto":
            raise RuntimeError("step_and_wait only valid for auto jobs")

        runner = self._jobs[job_id]
        state.current_step = (state.current_step or 0) + 1

        self._update_state(
            job_id,
            status="running",
            message=f"Executing step {state.current_step}",
        )

        try:
            await runner.execute_step(payload)
            self._update_state(
                job_id,
                status="waiting",
                message="Waiting for next step",
            )
        except Exception as e:
            self._update_state(job_id, status="failed", message=str(e))
            await self._finalize_job(job_id)
            raise

    # --------------------------------------------------------
    # Abort
    # --------------------------------------------------------

    async def abort_job(self, job_id: str) -> None:
        if job_id not in self._states:
            return

        state = self._states[job_id]
        if state.status in ("completed", "failed", "aborted"):
            return

        runner = self._jobs.get(job_id)
        if runner:
            runner.abort()

        self._update_state(job_id, status="aborted", message="Job aborted")
        await self._finalize_job(job_id)

    # --------------------------------------------------------
    # Internals
    # --------------------------------------------------------

    async def _manual_feeder(
        self,
        job_id: str,
        runner: JobRunner,
        steps: list[dict],
    ) -> None:
        """
        Background task executing all steps of a manual job sequentially.

        This coroutine:
        - runs independently of API request handlers
        - updates job state between steps
        - ensures robot resources are released on completion or failure
        """
        try:
            for idx, payload in enumerate(steps, start=1):
                self._update_state(
                    job_id,
                    status="running",
                    current_step=idx,
                    message=f"Executing step {idx}",
                )
                await runner.execute_step(payload)

            self._update_state(job_id, status="completed", message="Manual job completed")

        except asyncio.CancelledError:
            self._update_state(job_id, status="aborted", message="Manual job aborted")
            raise

        except Exception as e:
            self._update_state(job_id, status="failed", message=str(e))
            raise

        finally:
            await self._finalize_job(job_id)

    async def _create_runner(self, robot_id: str, protocol_name: str) -> JobRunner:
        """
        Create and start a JobRunner for the specified robot and protocol.
        """
        robot = self.robots[robot_id]
        key_path = self.access_dir / robot.key_name

        ot = OTRuntime(
            host=robot.host,
            user=robot.user,
            key_path=key_path,
        )

        runner = JobRunner(
            ot_runtime=ot,
            poll_interval=self.poll_interval,
        )
        await runner.start(protocol_name=protocol_name)
        return runner

    async def _finalize_job(self, job_id: str) -> None:
        """
        Finalize job execution.

        This method:
        - releases the robot lock
        - persists the final job state to long-term storage
        - cleans up temporary job files

        It is guaranteed to run exactly once per job.
        """
        state = self._states[job_id]
        runner = self._jobs.pop(job_id, None)

        lock = self._robot_locks[state.robot_id]
        if lock.locked():
            lock.release()

        self._persist_final_state(job_id)
        self._cleanup_job_files(job_id)

    # --------------------------------------------------------
    # Utilities
    # --------------------------------------------------------

    def _get_robot_lock(self, robot_id: str) -> asyncio.Lock:
        if robot_id not in self._robot_locks:
            raise KeyError(f"Unknown robot: {robot_id}")
        return self._robot_locks[robot_id]

    def _update_state(self, job_id: str, **changes) -> None:
        state = self._states[job_id]
        for k, v in changes.items():
            setattr(state, k, v)
        state.updated_at = time.time()
        self._persist_state(job_id)

    def _persist_state(self, job_id: str) -> None:
        """Write current job state to file."""
        pass

    def _persist_final_state(self, job_id: str) -> None:
        """Persist final job summary to DB or long-term storage."""
        pass

    def _cleanup_job_files(self, job_id: str) -> None:
        """Delete job working directory."""
        pass

    def _parse_instruction_yaml(self, path: Path, extract_dir: Path) -> list[dict]:
        """Parse instruction.yaml into list of step payloads.
            Not yet sure if it is yaml or json 
            and how it is organised - just started thinking of it.
        """
        raise NotImplementedError("Instruction YAML parsing not implemented yet")