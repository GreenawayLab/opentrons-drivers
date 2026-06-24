
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
