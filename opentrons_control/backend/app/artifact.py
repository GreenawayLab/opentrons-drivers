"""
Fetch-and-cache utility for SSH keys pulled from a remote store.

The store is reached via the configured ``base_url``, which may be either
an HTTP(S) endpoint or a local/network-mounted filesystem path. The class
handles both transparently.

Expected store layout::

    {base_url}/keys/{key_name}

Local cache layout::

    {cache_dir}/keys/{key_name}
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import requests


class Artifact:
    """
    Resolves named SSH keys from a remote store, caching them locally.

    Parameters
    ----------
    base_url :
        Root of the artifact store. Either an HTTP(S) URL or a local
        filesystem path (including network-mounted shares).
    cache_dir :
        Directory holding cached keys. Created if missing. Defaults to
        ``~/.opentrons_control/cache``.
    """

    def __init__(self, base_url: str, cache_dir: str | None = None) -> None:
        self.base_url = base_url.rstrip("/").rstrip("\\")
        self.cache_dir = Path(cache_dir or "~/.opentrons_control/cache").expanduser()
        (self.cache_dir / "keys").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_key(self, key_name: str) -> Path:
        """
        Resolve a named SSH key, caching it locally with mode 0600.

        Returns the local path. The file is guaranteed to be present and
        to have permissions accepted by ``ssh`` and ``scp``.
        """
        cached_key = self.cache_dir / "keys" / key_name

        if not cached_key.exists():
            self._fetch_key(key_name, cached_key)

        cached_key.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return cached_key

    def invalidate_key(self, key_name: str) -> None:
        """Remove the cached key for ``key_name``, forcing re-fetch."""
        cached_key = self.cache_dir / "keys" / key_name
        if cached_key.exists():
            cached_key.unlink()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_http(self) -> bool:
        return self.base_url.startswith("http://") or self.base_url.startswith("https://")

    def _fetch_key(self, key_name: str, dest: Path) -> None:
        """Fetch a key from the store into ``dest``, dispatching by transport."""
        if self._is_http():
            url = f"{self.base_url}/keys/{key_name}"
            self._download_http(url, dest)
        else:
            source = Path(self.base_url) / "keys" / key_name
            self._copy_file(source, dest)

    @staticmethod
    def _download_http(url: str, dest: Path) -> None:
        """Stream-download a file from ``url`` to ``dest`` atomically."""
        try:
            response = requests.get(url, stream=True, timeout=60)
            response.raise_for_status()
        except requests.RequestException as e:
            raise ConnectionError(f"Failed to download {url}: {e}") from e

        tmp = dest.with_suffix(".tmp")
        try:
            with open(tmp, "wb") as f:
                f.writelines(response.iter_content(chunk_size=8192))
            tmp.rename(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _copy_file(source: Path, dest: Path) -> None:
        """Copy a file from a local or network-mounted path."""
        if not source.exists():
            raise FileNotFoundError(f"Key not found at {source}")
        shutil.copy2(source, dest)