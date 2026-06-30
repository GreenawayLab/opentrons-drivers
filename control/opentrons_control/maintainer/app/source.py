"""
Fetch the drivers source as a ref tarball over HTTPS.

The maintainer downloads the repository archive for a ref from GitHub, 
extracts only the drivers subtree into a temporary directory, and builds from it. 
Authentication is an optional bearer token: absent -> unauthenticated request (public repo),
present -> authenticated (private repo). The same code path serves both, so flipping the
repo private is a config change, not a code change.

GitHub's tarball wraps everything in a single top-level ``<repo>-<sha>/``
directory; we extract only the members under ``<wrapper>/<DRIVERS_SUBDIR>/``,
so the control-plane source never even lands on disk.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import httpx

from opentrons_control.maintainer.app.config import DRIVERS_SUBDIR
from opentrons_control.maintainer.app.config import GITHUB_API_BASE
from opentrons_control.maintainer.app.config import GITHUB_REPO
from opentrons_control.maintainer.app.config import GIT_REF

#: Budget for the archive download. The repo archive is small; this is slack.
_DOWNLOAD_TIMEOUT = 120.0


class SourceError(RuntimeError):
    """Raised when the source archive cannot be fetched or extracted."""


def _download_tarball(token: str | None) -> bytes:
    """Download the repo archive for GIT_REF. Returns the raw .tar.gz bytes.

    Uses the GitHub API tarball endpoint, which 302-redirects to a signed
    codeload URL; ``follow_redirects`` chases it. A token, when present, is sent
    as a bearer header on the initial request.
    """
    if not GITHUB_REPO:
        raise SourceError("GITHUB_REPO is not configured")
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/tarball/{GIT_REF}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT) as client:
            r = client.get(url, headers=headers)
            r.raise_for_status()
            return r.content
    except httpx.HTTPStatusError as e:
        raise SourceError(
            f"archive fetch returned {e.response.status_code} "
            f"for {GITHUB_REPO}@{GIT_REF}"
        ) from e
    except httpx.HTTPError as e:
        raise SourceError(f"archive fetch failed: {e}") from e


def fetch_source(token: str | None, dest: Path) -> Path:
    """Download the repo tarball and extract only the drivers subtree into ``dest``.

    :param token: Optional bearer token; None fetches unauthenticated (public).
    :param dest: Directory to extract into (created if missing).
    :returns: Path to the extracted drivers project (the dir with pyproject.toml).
    :raises SourceError: if the download, extraction, or drivers lookup fails.
    """
    dest.mkdir(parents=True, exist_ok=True)
    data = _download_tarball(token)

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            names = tar.getnames()
            if not names:
                raise SourceError("archive is empty")
            wrapper = names[0].split("/", 1)[0]
            prefix = f"{wrapper}/{DRIVERS_SUBDIR}/"
            members = [m for m in tar.getmembers() if m.name.startswith(prefix)]
            if not members:
                raise SourceError(
                    f"{DRIVERS_SUBDIR!r} not found in archive; check DRIVERS_SUBDIR"
                )
            # filter='data' (3.12+) blocks path-traversal / unsafe members.
            tar.extractall(dest, members=members, filter="data")
    except tarfile.TarError as e:
        raise SourceError(f"archive extraction failed: {e}") from e

    project = dest / wrapper / DRIVERS_SUBDIR
    if not (project / "pyproject.toml").exists():
        raise SourceError(f"no pyproject.toml at {project}; check DRIVERS_SUBDIR")
    return project