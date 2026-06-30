"""
Maintainer configuration.

A thin service, configured like the proxy/frontend via environment variables.
The maintainer owns the wheel store and the build, fetches the drivers source
as a tarball over HTTPS, talks to the backend for the (optional) git token and
for install execution, and never touches a robot directly.
"""

from __future__ import annotations

import os

#: Backend base URL (token fetch + install execution).
BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")

#: Outbound timeout for backend calls. Generous: a fleet install blocks until
#: every targeted robot has finished its pip install.
BACKEND_TIMEOUT = float(os.environ.get("BACKEND_TIMEOUT", "600"))

#: Persistent store for built wheels, laid out as <WHEELS_DIR>/<version>/.
WHEELS_DIR = os.environ.get("WHEELS_DIR", "/data/wheels")

#: GitHub repository to build from, as "owner/name".
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

#: GitHub API base. Override only for a GitHub Enterprise / mirror host.
GITHUB_API_BASE = os.environ.get("GITHUB_API_BASE", "https://api.github.com").rstrip("/")

#: Branch, tag, or commit SHA to build from.
GIT_REF = os.environ.get("GIT_REF", "main")

#: Repo-root subdirectory holding the drivers pyproject.toml.
DRIVERS_SUBDIR = os.environ.get("DRIVERS_SUBDIR", "opentrons_drivers")

#: Backend endpoints the maintainer calls.
TOKEN_PATH = "/internal/update/token"
INSTALL_PATH = "/internal/update"