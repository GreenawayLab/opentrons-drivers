from dataclasses import dataclass, field
from typing import Literal, Optional
import time

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