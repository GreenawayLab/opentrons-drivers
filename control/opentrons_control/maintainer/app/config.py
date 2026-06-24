"""
Maintainer configuration.

A thin service, configured like the proxy/frontend via environment variables
rather than a settings file. The maintainer owns the wheel store and the
build, talks to the backend over HTTP for credentials and installs, and never
touches a robot directly.
"""

from __future__ import annotations

import os

#: Backend base URL (credential fetch + install execution).
BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")

#: Outbound timeout for backend calls. Generous: a fleet install blocks until
#: every targeted robot has finished its pip install.
BACKEND_TIMEOUT = float(os.environ.get("BACKEND_TIMEOUT", "600"))

#: Persistent store for built wheels, laid out as <WHEELS_DIR>/<version>/.
WHEELS_DIR = os.environ.get("WHEELS_DIR", "/data/wheels")

#: Working directory the drivers source is cloned into (wiped per build).
SOURCE_DIR = os.environ.get("SOURCE_DIR", "/data/src")

#: Subdirectory within the cloned repo that holds the drivers pyproject.toml.
DRIVERS_SUBDIR = os.environ.get("DRIVERS_SUBDIR", "opentrons_drivers")

#: Git remote for the monorepo, in SSH form for deploy-key auth, e.g.
#: git@github.com:org/opentrons-lab.git
REPO_URL = os.environ.get("REPO_URL", "")

#: Branch or tag to build from.
GIT_REF = os.environ.get("GIT_REF", "main")

#: tmpfs directory the git deploy key is written to during a clone.
KEYS_DIR = os.environ.get("KEYS_DIR", "/run/keys")

#: Backend endpoints the maintainer calls.
GIT_CREDENTIAL_PATH = "/internal/update/credential"
INSTALL_PATH = "/internal/update"