from pathlib import Path

#: Statuses returned by GET /health that mean the agent is fully operational. Can be expanded.
HEALTHY_STATUSES = ("ready",)

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

#: Wall-clock budget for the agent to report ready after launch. Hardware
#: boot typically takes 60-80 seconds; the headroom covers slow USB
#: enumeration and pipette discovery.
DEFAULT_READINESS_TIMEOUT = 180.0

# Config operational location

DEFAULT_CONFIG_PATH = "/data/backend.json"


# -------------------- Driver update --------------------
#
# The backend is a pure executor: it does not persist wheels (the maintainer
# owns the wheel store). The drivers wheel installs with plain pip from a
# local file — opentrons (its only dependency) is already on the robot, so no
# package index is needed.

#: Distribution name of the on-robot drivers wheel (pip uninstall target).
DRIVERS_PACKAGE = "opentrons_drivers"

#: pip on the robot. Set to "python3 -m pip" if bare pip resolves to a
#: different interpreter than the one opentrons_execute runs.
ROBOT_PIP = "pip"

#: Scratch dir on the robot the wheel is uploaded to, installed from, and
#: then removed from.
WHEEL_STAGING_DIR = "/data/driver_updates"

#: Upper bound on an accepted wheel upload. The drivers wheel is pure-python
#: and tiny; this only guards against a runaway upload tying up memory.
MAX_WHEEL_BYTES = 50 * 1024 * 1024