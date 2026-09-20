"""The caches a confined Python run shares with the person's own tools.

A jailed child may write to the workspace it was given and to Vis' own state
directory, and to nothing else. uv, Poetry and pip keep their downloads and
their managed interpreters outside both, so a confined run would re-download
everything into a directory it may not even create. These are the exact
directories the extension grants on the call that starts a run; the jail
refuses the rest, unchanged.
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


def granted_paths(*extra: str) -> tuple[str, ...]:
    """Paths outside the session that a Python run is handed.

    Args:
        extra: Further directories this one call needs, such as the private
            directory a report is written to.

    Returns:
        The directories to grant, as a tuple.
    """
    return (
        uv_cache(),
        uv_interpreters(),
        poetry_cache(),
        pip_cache(),
        *(str(path) for path in extra),
    )
