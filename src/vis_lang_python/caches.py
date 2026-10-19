"""The caches a confined Python run shares with the person's own tools.

A jailed child can write only to its workspace and to Vis' own state directory.
uv, Poetry and pip keep their downloads and managed interpreters outside both. A
confined run would download everything again, into a directory that it may not
be allowed to create. The extension declares these exact directories as
read-write grants. Vis gives them only to this extension's own processes. The
jail still refuses everything else.
"""

from __future__ import annotations

import os
from pathlib import Path


def _directory(variable: str, *fallback: str) -> str:
    """The directory `variable` names, else `fallback` under the person's home."""
    override = os.environ.get(variable, "").strip()
    return override or str(Path.home().joinpath(*fallback))


def uv_cache() -> str:
    """uv's download cache: `UV_CACHE_DIR`, else the ordinary `~/.cache/uv`."""
    return _directory("UV_CACHE_DIR", ".cache", "uv")


def uv_interpreters() -> str:
    """Where uv keeps the interpreters it manages.

    `uv run python` installs the version a project asks for here, so a run
    confined without it downloads a whole interpreter it then cannot keep.

    Returns:
        The directory `UV_PYTHON_INSTALL_DIR` names, else
        `~/.local/share/uv/python`.
    """
    return _directory("UV_PYTHON_INSTALL_DIR", ".local", "share", "uv", "python")


def poetry_cache() -> str:
    """Poetry's cache: `POETRY_CACHE_DIR`, else `~/.cache/pypoetry`."""
    return _directory("POETRY_CACHE_DIR", ".cache", "pypoetry")


def pip_cache() -> str:
    """pip's wheel cache: `PIP_CACHE_DIR`, else `~/.cache/pip`."""
    return _directory("PIP_CACHE_DIR", ".cache", "pip")


def cache_paths() -> tuple[str, ...]:
    """The directories that a confined Python run shares with the person's tools.

    Returns:
        The uv cache, uv's interpreters, the Poetry cache and the pip cache.
    """
    return (uv_cache(), uv_interpreters(), poetry_cache(), pip_cache())
