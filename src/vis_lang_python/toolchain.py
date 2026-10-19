"""The programs a confined Python run starts, and the files they resolve to.

A jailed child reads only the system directories, its workspace and the
directories that it is granted. Launchers such as `vis-agent` and `uv` are often
installed under the person's home, for example in `~/.local/bin`. The jail then
refuses to execute them. The extension declares these exact files as read-only
toolchain grants. The host checks each grant and gives it only to this
extension's own processes, not to the agent sandbox.
"""

from __future__ import annotations

import os
import shutil

COMMANDS = ("vis-agent", "uv", "poetry", "python3", "python", "ruff")
"""Programs that a REPL, a test run or a lint run can start."""


def _covers(path: str, protected: str) -> bool:
    """Whether `path` is `protected` or one of its parent directories."""
    return protected == path or protected.startswith(path.rstrip(os.sep) + os.sep)


def grantable(paths) -> tuple[str, ...]:
    """The paths of `paths` that the host accepts as grants, in order, once each.

    The host refuses a grant whose resolved path is the filesystem root, or that
    contains the home directory or the Vis home. Such a grant would stop the
    extension from registering, so it is left out here. Relative and missing
    paths are left out too.

    Args:
        paths: Candidate paths.

    Returns:
        The kept paths, as declared.
    """
    home = os.path.realpath(os.path.expanduser("~"))
    vis_home = os.path.realpath(
        os.environ.get("VIS_HOME", "").strip() or os.path.join(home, ".vis")
    )
    kept: list[str] = []
    for path in paths:
        if not path or not os.path.isabs(path) or not os.path.exists(path):
            continue
        declared = os.path.normpath(path)
        resolved = os.path.realpath(declared)
        if os.path.dirname(resolved) == resolved:
            continue
        if _covers(resolved, home) or _covers(resolved, vis_home):
            continue
        if declared not in kept:
            kept.append(declared)
    return tuple(kept)


def toolchain_paths() -> tuple[str, ...]:
    """Each program of `COMMANDS` on PATH, as found and as its links resolve.

    The jail follows a link only when it may read the link itself, so both the
    found path and its target are kept.

    Returns:
        The files to grant read access to.
    """
    found: list[str] = []
    for name in COMMANDS:
        path = shutil.which(name)
        if path:
            found += [os.path.abspath(path), os.path.realpath(path)]
    return grantable(found)
