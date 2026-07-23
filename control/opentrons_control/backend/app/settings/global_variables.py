#: Statuses returned by GET /health that mean the agent is fully operational. Can be expanded.
HEALTHY_STATUSES = ("ready",)

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
#:
#: PYTHONUTF8 / LC_ALL pin the whole agent process to UTF-8. The robot's
#: inherited locale is not guaranteed UTF-8, so without this a plate file
#: containing "µL" decodes wrong (or raises UnicodeDecodeError) the moment
#: the agent reads it — and it manifests per-robot, depending on each
#: robot's locale. Pinning here fixes every file read in the process at
#: once, rather than per-open(); that is why a single-site encoding= fix
#: tends to leak back. PYTHONUTF8=1 forces Python's UTF-8 mode (all open(),
#: json, filesystem ops); LC_ALL covers any non-Python child in the tree.
AGENT_ENV = {
    "RUNNING_ON_PI": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONUTF8": "1",
    "LC_ALL": "C.UTF-8",
}

#: Wall-clock budget for the agent to report ready after launch. Hardware
#: boot typically takes 60-80 seconds; the headroom covers slow USB
#: enumeration and pipette discovery.
DEFAULT_READINESS_TIMEOUT = 180.0

# Config operational location

DEFAULT_CONFIG_PATH = "/data/backend.json"


# -------------------- Robot environment --------------------
#
# Where the drivers package lives on the OT is NOT hardcoded to a Python
# version. The install location is whatever pip uses by default, discovered
# at runtime via `pip show` (see bootstrap.start_agent). The only fixed part
# is the relative path of agent_main.py inside the installed package.

#: Distribution / import name of the on-robot drivers package.
DRIVERS_PACKAGE = "opentrons_drivers"

#: Explicit interpreter path on the robot. Empty means detect it at runtime,
#: see bootstrap.detect_python. Set this only to pin a specific interpreter,
#: for example when a robot has several and the probe picks the wrong one.
ROBOT_PYTHON = ""

#: Interpreter candidates tried in order when ROBOT_PYTHON is empty. The first
#: whose "-m pip" actually responds wins. Bare "pip" is deliberately absent as a
#: candidate: it is missing on some robot images, which is what makes runtime
#: detection necessary in the first place.
PYTHON_CANDIDATES = ("python3", "python", "/usr/bin/python3")

#: Path of the agent entry point relative to the installed package's
#: location (i.e. relative to `pip show`'s reported Location).
AGENT_MAIN_RELPATH = f"{DRIVERS_PACKAGE}/agent/agent_main.py"


# -------------------- Driver update --------------------
#
# The backend is a pure executor: it does not persist wheels (the maintainer
# owns the wheel store). The drivers wheel installs with plain pip from a
# local file — opentrons (its only dependency) is already on the robot, so no
# package index is needed.

#: Scratch dir on the robot the wheel is uploaded to, installed from, and
#: then removed from.
WHEEL_STAGING_DIR = "/data/driver_updates"

#: Upper bound on an accepted wheel upload. The drivers wheel is pure-python
#: and tiny; this only guards against a runaway upload tying up memory.
MAX_WHEEL_BYTES = 50 * 1024 * 1024

#: Vault secret name for the git access token (a read-only PAT) the maintainer
#: uses to fetch the repo archive. Optional: if unset, public repos still work
#: and the maintainer fetches unauthenticated. Store it with:
#:   store_secret git_token git_token --file ./token
GIT_TOKEN_SECRET = "git_token"