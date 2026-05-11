from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Dict
from ssh_handler import OTRuntime
from states import Robot, JobState, Mode
from runner import JobRunner

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
        self._jobs.pop(job_id, None)

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