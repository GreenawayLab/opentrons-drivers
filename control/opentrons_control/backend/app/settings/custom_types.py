from typing import TypedDict, Dict, List, Union, NotRequired, Optional, Literal

#------------ JSON Type Definitions ------------

# We strictly define that the content of the arguments 
# passed to the functions must be something serialisable to JSON.

JSONScalar = Union[str, int, float, bool, None]
JSONType = Union[JSONScalar, List["JSONType"], Dict[str, "JSONType"]]

# -------------------- Submit for ot_client --------------------

class JobSnapshotDict(TypedDict, total=False):
    job_id: Optional[str]
    action: Optional[str]
    status: Optional[str]
    error: Optional[str]
    result: JSONType
    submitted_at: Optional[float]
    finished_at: Optional[float]

# -------------------- Exceptions for ot_client --------------------

class OTClientError(RuntimeError):
    """Base class for agent client errors."""


class AgentBusy(OTClientError):
    """Raised when the agent rejects a submission because its slot is occupied."""

    def __init__(self, info: dict[str, JSONType]):
        super().__init__(f"agent busy: {info}")
        self.info = info


class AgentNotReady(OTClientError):
    """Raised when the agent returns 503 (hardware not finished initialising)."""


class AgentBadRequest(OTClientError):
    """Raised when the agent returns 400 for a malformed submission."""

    def __init__(self, info: dict[str, JSONType]):
        super().__init__(f"agent rejected request: {info}")
        self.info = info


class AgentUnreachable(OTClientError):
    """Raised when the underlying HTTP transport fails (connection refused, timeout, etc.)."""


class JobNotFound(OTClientError):
    """Raised when a polled job_id is unknown to the agent."""


# -------------------- Exceptions for launcher --------------------


class BootstrapFailed(RuntimeError):
    """
    Raised when a session cannot reach the ``active`` status.

    The session is left cleaned up (marked ``failed``, lock released)
    before the exception propagates.
    """


class FileFormatError(ValueError):
    """Raised when a file entry cannot be serialised as written."""


# -------------------- Sessions types --------------------


Mode = Literal["manual", "auto"]

SessionStatus = Literal[
    "starting",   # bootstrap in flight (SSH + agent boot)
    "active",     # agent ready, accepting actions through the proxy
    "aborting",   # abort received, teardown in flight
    "ended",      # terminal: completed normally or fully torn down
    "failed",     # terminal: bootstrap or runtime failure
]

TERMINAL_STATUSES: tuple[SessionStatus, ...] = ("ended", "failed")


# -------------------- Sessions exceptions --------------------


class UnknownRobot(KeyError):
    """Raised when an operation references a robot_id not in the registry."""


class RobotBusy(RuntimeError):
    """Raised when a session cannot be created because the robot is in use."""

    def __init__(self, robot_id: str):
        super().__init__(f"robot {robot_id!r} is busy")
        self.robot_id = robot_id


class UnknownSession(KeyError):
    """Raised when an operation references an unknown session token."""


# -------------------- Backend main --------------------

class ArtifactsConfig(TypedDict):
    base_url: str

class RobotEntry(TypedDict):
    host: str
    user: str
    key_name: str
    agent_port: NotRequired[int]

class BackendConfig(TypedDict):
    artifacts: ArtifactsConfig
    robots: Dict[str, RobotEntry]