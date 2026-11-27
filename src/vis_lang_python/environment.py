"""The project's dependencies, installed before its interpreter starts.

A REPL start installs what the project declares, so that the interpreter sees
the packages that the files name. `pyproject.toml` wins over `requirements.txt`.
The installer is the uv that Vis ships (`vis-agent python uv`), and a `uv` on
PATH only when the process has no Vis. Vis itself says where it is, so it does
not need to be on PATH. With `python.environment: shared`, the packages go to
Vis shared packages and no `.venv` is made.

An installation costs a process, and Vis can take seconds to start. So a
successful installation leaves a stamp: a digest of the dependency files and of
the distributions in the environment. While both stay the same, the next start
runs no installer at all.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from vis_lang_interface import process

try:
    import fcntl
except ImportError:  # No POSIX locks: starts are not serialized there.
    fcntl = None

from vis_lang_python.caches import sync_stamps

SYNC_TIMEOUT_S = 900
"""Seconds one installation can take. Downloads and builds need far longer than
the interpreter's own startup."""

FAILURE_LIMIT = 490
"""Maximum size of the installer summary, before the REPL adds its own text."""

STAMP_VERSION = 1
HASHED_BYTES = 4 * 1024 * 1024
"""Files up to this size count by content, larger ones by size and time."""

PRINT_EXECUTABLE = "import sys; print(sys.executable)"
NO_INSTALLER = "found no Vis, and uv is not on PATH"
REMADE = "the `.venv` had no interpreter, so uv made it again"
ENSURE_PIP = ("-m", "ensurepip", "--default-pip")
NO_PIP = "pip was not added"
OFFLINE = "UV_OFFLINE is set, so uv asked no index"
CHANGES_SHOWN = 8
"""How many changed packages the result of an upgrade names."""
STALE = (
    "the project's dependency files changed after this REPL started, or their"
    " installation failed. Call py.repl_stop(), then py.repl_start() to install"
    " them. That ends the REPL's globals"
)

BUILD_FILES = ("pyproject.toml", "setup.py", "setup.cfg")
PROJECT_FILES = ("pyproject.toml", "uv.lock", ".python-version")
DISTRIBUTIONS = (".dist-info", ".egg-info", ".egg-link", ".pth")
LOCAL_SOURCE = re.compile(r'\b(?:editable|directory|virtual|path)\s*=\s*"([^"]+)"')
INCLUDED = re.compile(r"^\s*(?:-r|--requirement|-c|--constraint)(?:\s+|=)(\S+)")
LOCAL_REQUIREMENT = re.compile(
    r"^\s*(?:(?:-e|--editable)(?:\s+|=))?(\.{0,2}/[^\s;\[]*|\.)"
)


@dataclasses.dataclass(frozen=True)
class Sync:
    """What one start did about the project's dependencies.

    `state` is `installed`, `upgraded` (installed with the newest versions that
    the files allow), `current` (nothing changed since the last installation),
    `none` (the project declares nothing to install), `skipped` (no installer)
    or `failed`.
    """

    state: str
    tool: str = ""
    command: tuple[str, ...] = ()
    seconds: float = 0.0
    exit_code: int | None = None
    output: tuple[str, ...] = ()
    reason: str = ""
    note: str = ""
    """What the installer did besides the installation, for the person."""
    changes: tuple[str, ...] = ()
    """The packages whose version an upgrade changed, as `name old -> new`."""
    log: str = ""


@dataclasses.dataclass(frozen=True)
class Environment:
    """The interpreter of a prepared project, and how it was prepared.

    `python` is the argv prefix that launches the interpreter. It is empty when
    the caller chooses the interpreter itself, and when a failed installation
    left no environment to run. `inputs` is the digest of the dependency files
    that this environment holds, empty when the installation failed, and None
    when dependency sync is disabled.
    """

    python: tuple[str, ...]
    sync: Sync
    inputs: str | None = ""


@dataclasses.dataclass(frozen=True)
class _Plan:
    kind: str
    """`pyproject` or `requirements`."""
    uv: tuple[str, ...]
    """The argv prefix of the installer, empty when there is none."""
    tool: str
    sources: tuple[str, ...] = ()
    """What `uv pip install` reads from the `pyproject.toml` of a project that
    uv does not own. Empty for a uv project."""


def venv_python(root: Path) -> str | None:
    """ABSOLUTE path of a project-local virtualenv's interpreter, or None.

    Never RESOLVED: `.venv/bin/python3` is a symlink chain that ends at the base
    installation. Following it leaves the virtualenv. Then `sys.prefix` becomes
    the base prefix and `pyvenv.cfg` is never read. The run fails with
    `No module named pytest`, while the same suite passes under
    `.venv/bin/python`.
    """
    for venv in (".venv", "venv"):
        for name in ("bin/python", "bin/python3"):
            candidate = root / venv / name
            if candidate.is_file():
                return str(candidate)
    return None


def pyproject_tables(root: Path) -> dict:
    """The tables of the project's `pyproject.toml`, read as TOML. Empty when the
    file is absent or does not parse."""
    try:
        import tomllib

        with open(root / "pyproject.toml", "rb") as handle:
            return tomllib.load(handle)
    except Exception:
        return {}


def is_uv_marked(root: Path, tables: dict | None = None) -> bool:
    """A `uv.lock`, or a real `[tool.uv]` table in `pyproject.toml`, read as
    TOML and never as a substring search. `[tool.uvicorn]`, a commented-out
    `[tool.uv]` or a description that only mentions one is not a uv project."""
    if (root / "uv.lock").is_file():
        return True
    tool = (pyproject_tables(root) if tables is None else tables).get("tool")
    return isinstance(tool, dict) and isinstance(tool.get("uv"), dict)


def installer(shared: bool = False) -> tuple[tuple[str, ...], str]:
    """The argv prefix of the uv to install with, and its name for people.

    The uv that Vis ships comes first, through the launcher that Vis names. A
    `uv` on PATH serves a project environment only when the process has no Vis.
    Shared packages have no other installer than Vis itself.
    """
    found = process.vis_python()
    if found.is_available:
        return tuple(found.uv(shared=shared)), "uv from Vis"
    if not shared and shutil.which("uv"):
        return ("uv",), "uv from PATH"
    return (), ""


def _plan(root: Path, shared: bool) -> _Plan | None:
    """What to install for `root`, or None when it declares nothing.

    A `pyproject.toml` counts when it declares dependencies: a `[project]` table,
    a `dev` group in `[dependency-groups]`, or a `uv.lock` next to it. uv installs
    no other group by itself. A file with only
    tool configuration does not, so `requirements.txt` next to it is installed.
    That includes a `[tool.uv]` table, which there only configures uv; without
    `requirements.txt` it is a uv project. A Poetry project is not touched, also
    when `poetry` is absent: uv would write its own lock into it.
    """
    tables = pyproject_tables(root)
    marked = is_uv_marked(root, tables)
    if not marked and (root / "poetry.lock").is_file():
        return None
    sources = _sources(tables)
    if sources or (root / "uv.lock").is_file():
        kind = "pyproject"
    elif (root / "requirements.txt").is_file():
        kind = "requirements"
    elif marked:
        kind = "pyproject"
    else:
        return None
    uv, tool = installer(shared)
    return _Plan(kind, uv, tool, sources if kind == "pyproject" and not marked else ())


def _sources(tables: dict) -> tuple[str, ...]:
    """The `uv pip install` arguments for what `uv run` installs from a
    `pyproject.toml`: the project's dependencies and its `dev` group."""
    sources: list[str] = []
    if isinstance(tables.get("project"), dict):
        sources += ["-r", "pyproject.toml"]
    groups = tables.get("dependency-groups")
    if isinstance(groups, dict) and "dev" in groups:
        sources += ["--group", "dev"]
    return tuple(sources)


def _digest(parts) -> str:
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _mark(path: Path):
    """What identifies the content of `path`, or None when it is no file."""
    try:
        status = path.stat()
        if not path.is_file():
            return None
        if status.st_size > HASHED_BYTES:
            return [status.st_size, status.st_mtime_ns]
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _local_files(base: Path, source: str) -> list[Path]:
    """The files that decide what a local path source installs."""
    target = base / source
    if target.is_dir():
        return [target / name for name in BUILD_FILES]
    return [target]


def _project_files(root: Path) -> list[Path]:
    """`pyproject.toml`, `uv.lock` and `.python-version` of the project and of
    each parent, which can be its workspace root, and the local path sources
    that a lock names."""
    files: list[Path] = []
    for directory in (root, *root.parents):
        files += [directory / name for name in PROJECT_FILES]
        try:
            lock = (directory / "uv.lock").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for source in LOCAL_SOURCE.findall(lock):
            files += _local_files(directory, source)
    return files


def _requirement_files(root: Path) -> list[Path]:
    """`requirements.txt`, each file that it includes with `-r` or `-c`, and the
    build files of the local directories that it installs."""
    files: list[Path] = []
    waiting = [root / "requirements.txt"]
    seen: set[str] = set()
    while waiting:
        path = waiting.pop()
        key = os.path.normpath(str(path))
        if key in seen:
            continue
        seen.add(key)
        files.append(path)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            included = INCLUDED.match(line)
            if included:
                # pip reads an included file relative to the file that names it.
                waiting.append(path.parent / included.group(1))
                continue
            local = LOCAL_REQUIREMENT.match(line)
            if local:
                files += _local_files(root, local.group(1))
    return files


def _inputs(root: Path, plan: _Plan, shared: bool) -> str:
    files = (
        _project_files(root) if plan.kind == "pyproject" else _requirement_files(root)
    )
    marks = sorted((os.path.normpath(str(path)), _mark(path)) for path in set(files))
    return _digest(
        [
            STAMP_VERSION,
            "shared" if shared else "project",
            plan.kind,
            [[path, mark] for path, mark in marks if mark is not None],
        ]
    )


def _root(cwd: str) -> Path:
    """The project directory under one spelling. A link in the path, such as
    `/var` on macOS, must not give the same project two digests."""
    return Path(os.path.realpath(os.path.expanduser(str(cwd))))


def inputs(cwd: str, *, shared: bool = False) -> str:
    """The digest of the dependency files that `cwd` declares, or "" when it
    declares nothing to install. No process runs."""
    root = _root(cwd)
    plan = _plan(root, shared)
    return _inputs(root, plan, shared) if plan else ""


def _distributions(directory: Path):
    """The distributions in one packages directory, by the names of their
    metadata. An installation, an upgrade or a removal changes this list. An
    import that writes `__pycache__` does not."""
    try:
        with os.scandir(directory) as entries:
            return sorted(
                entry.name for entry in entries if entry.name.endswith(DISTRIBUTIONS)
            )
    except OSError:
        return None


def shared_packages() -> Path:
    """The directory of Vis shared packages, as the Vis that hosts this process
    names it."""
    return process.vis_python().shared_packages


def _state(python: tuple[str, ...], shared: bool) -> str:
    """The digest of the environment that `python` runs in."""
    if shared:
        return _digest(["shared", list(python), _distributions(shared_packages())])
    home = Path(python[0]).parent.parent
    packages = sorted(home.glob("lib/python*/site-packages"))
    return _digest(
        [
            list(python),
            _mark(home / "pyvenv.cfg"),
            [[str(path), _distributions(path)] for path in packages],
        ]
    )


def _versions(python: tuple[str, ...], shared: bool) -> dict[str, str]:
    """The version of each distribution in the environment that `python` runs
    in. Empty when no environment is there."""
    if shared:
        directories = [shared_packages()]
    elif len(python) == 1:
        home = Path(python[0]).parent.parent
        directories = sorted(home.glob("lib/python*/site-packages"))
    else:
        directories = []
    found: dict[str, str] = {}
    for directory in directories:
        for name in _distributions(directory) or ():
            distribution, dash, version = name.rpartition(".")[0].rpartition("-")
            if dash and name.endswith(".dist-info"):
                found[distribution] = version
    return found


def _changes(before: dict[str, str], after: dict[str, str]) -> tuple[str, ...]:
    """What an upgrade changed. uv exits with status 0 also when it found
    nothing newer, and when `UV_OFFLINE` kept it from the index."""
    return tuple(
        f"{name} {before[name]} -> {version}" if name in before else f"{name} {version}"
        for name, version in sorted(after.items())
        if before.get(name) != version
    )


def _is_offline(env) -> bool:
    """Whether the installer runs with `UV_OFFLINE`."""
    value = {**os.environ, **(env or {})}.get("UV_OFFLINE")
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _is_usable(python: tuple[str, ...]) -> bool:
    return bool(python) and os.path.isfile(python[0]) and os.access(python[0], os.X_OK)


def _stamp_file(root: Path, shared: bool) -> Path:
    key = _digest([os.path.realpath(root), "shared" if shared else "project"])[:24]
    return Path(sync_stamps()) / f"{key}.json"


def _read_stamp(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get("version") != STAMP_VERSION:
        return {}
    return value


def _write_stamp(path: Path, value: dict) -> None:
    """Keep the stamp. A stamp that cannot be written costs only the next
    start's installer run, so a refused write is not an error."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        partial.write_text(json.dumps(value), encoding="utf-8")
        os.replace(partial, path)
    except OSError:
        pass


def _stamped_python(stamp: dict) -> tuple[str, ...]:
    python = stamp.get("python")
    if isinstance(python, list) and all(isinstance(part, str) for part in python):
        return tuple(python)
    return ()


def _run(command, root: Path, env, timeout_s) -> process.ToolRun:
    """One installer command, to completion."""
    return process.run(list(command), cwd=str(root), timeout_s=timeout_s, env=env)


def _diagnostics(done: process.ToolRun) -> tuple[str, ...]:
    """Keep a cause and the last diagnostic lines for the summary."""
    lines = [
        line.strip() for line in f"{done.out}\n{done.err}".splitlines() if line.strip()
    ]
    tail = lines[-2:]
    cause = next(
        (line for line in lines if line.lower().startswith(("error:", "failed"))), ""
    )
    return tuple(dict.fromkeys(([cause] if cause else []) + tail[::-1]))


def _save_failure_log(output: list[str]) -> str:
    """Keep the captured installer streams in the session's temporary directory."""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="python-install-",
            suffix=".log",
            delete=False,
        ) as log:
            log.writelines(output)
            return log.name
    except OSError:
        return ""


def _steps(
    root: Path, plan: _Plan, shared: bool, upgrade: bool = False
) -> tuple[list[tuple[str, ...]], tuple[str, ...], bool]:
    """The installer commands for `plan`, the interpreter they prepare, and
    whether the first command makes the `.venv`. The interpreter is empty when
    the last command prints it. With `upgrade`, uv takes the newest versions
    that the files allow, and not the ones that are installed or locked."""
    uv = plan.uv
    newest = ("--upgrade",) if upgrade else ()
    if shared:
        launcher = uv[:-1]
        if plan.kind == "requirements":
            install = (*uv, "pip", "install", *newest, "-r", "requirements.txt")
            return [install], launcher, False
        if plan.sources:
            # `uv sync` would write a `uv.lock` into a project that uv does not own.
            return [(*uv, "pip", "install", *newest, *plan.sources)], launcher, False
        # Vis refuses `uv sync --upgrade` for shared packages, so the lock is
        # upgraded first.
        lock = [(*uv, "lock", "--upgrade")] if upgrade else []
        return [*lock, (*uv, "sync")], launcher, False
    if plan.kind == "pyproject":
        python = venv_python(root) if plan.sources else None
        if python:
            # The person made this environment and uv does not own the project.
            # `uv run` would lock the project, make a `.venv` next to a `venv`,
            # and make a `.venv` again when its Python does not fit.
            install = (
                *uv,
                "pip",
                "install",
                *newest,
                "--python",
                python,
                *plan.sources,
            )
            return [install], (python,), False
        # `uv run` installs like `uv sync --inexact`: packages that the person
        # added by hand stay. It then names the interpreter of the environment,
        # which can be the one of a workspace root.
        return [(*uv, "run", *newest, "python", "-c", PRINT_EXECUTABLE)], (), False
    steps: list[tuple[str, ...]] = []
    python = venv_python(root)
    makes_venv = python is None
    if makes_venv:
        # Without `--clear`, uv asks a question when a broken `.venv` exists.
        steps.append((*uv, "venv", "--clear", ".venv"))
        python = str(root / ".venv" / "bin" / "python")
    steps.append(
        (*uv, "pip", "install", *newest, "--python", python, "-r", "requirements.txt")
    )
    return steps, (python,), makes_venv


def _add_pip(python: tuple[str, ...], root: Path, env, timeout_s) -> str:
    """Give a virtualenv without pip the pip of its own Python, so that
    `python -m pip install` works in the REPL. That pip needs no index; the
    seed of uv does. Returns what to tell the person when it fails, else ""."""
    if len(python) != 1:
        return ""
    home = Path(python[0]).parent.parent
    if not (home / "pyvenv.cfg").is_file():
        return ""
    for packages in home.glob("lib/python*/site-packages"):
        if any(name.startswith("pip-") for name in _distributions(packages) or ()):
            return ""
    try:
        done = _run((python[0], *ENSURE_PIP), root, env, timeout_s)
    except Exception as ex:
        return f"{NO_PIP}: {ex}"
    if done.is_ok:
        return ""
    return f"{NO_PIP}: `python -m ensurepip` exited with status {done.exit_code}"


def _existing(
    root: Path, stamp: dict, shared: bool, launcher: tuple[str, ...]
) -> tuple[str, ...]:
    """The environment that exists before the installer runs, or ().

    It is read before the installation, because a failed installer leaves the
    empty `.venv` that it made. That one holds none of the project's packages.
    """
    if shared:
        return launcher
    stamped = _stamped_python(stamp)
    if _is_usable(stamped):
        return stamped
    python = venv_python(root)
    return (python,) if python else ()


def prepare(
    cwd: str,
    *,
    shared: bool = False,
    upgrade: bool = False,
    env=None,
    timeout_s=SYNC_TIMEOUT_S,
) -> Environment:
    """Install what the project in `cwd` declares, when it is not installed yet.

    Args:
        cwd: The project directory.
        shared: Install into Vis shared packages, not into a project `.venv`.
        upgrade: Install the newest versions that the files allow, also when
            nothing changed.
        env: Variables for the installer, over the ones it inherits.
        timeout_s: Seconds one installer command can take.

    Returns:
        The environment and what was done. A failed installation never raises:
        its `sync` carries the installer's command, exit status and last output.
    """
    root = _root(cwd)
    plan = _plan(root, shared)
    if plan is None:
        return Environment((), Sync("none"))
    if not plan.uv:
        return Environment(
            (), Sync("skipped", reason=NO_INSTALLER), _inputs(root, plan, shared)
        )
    stamp_file = _stamp_file(root, shared)
    stamp = _read_stamp(stamp_file)
    python = _stamped_python(stamp)
    if (
        not upgrade
        and python
        and (shared or _is_usable(python))
        and stamp.get("inputs") == _inputs(root, plan, shared)
        and stamp.get("state") == _state(python, shared)
    ):
        return Environment(python, Sync("current", plan.tool), stamp["inputs"])

    existing = _existing(root, stamp, shared, plan.uv[:-1])
    before = _versions(existing, shared) if upgrade else {}
    steps, python, makes_venv = _steps(root, plan, shared, upgrade)
    # What a failed installer leaves is removed, but only what this start made.
    leftovers = [
        path for path in (root / ".venv", root / "uv.lock") if not os.path.lexists(path)
    ]
    finished = 0
    is_remade = makes_venv and os.path.lexists(root / ".venv")
    started = time.monotonic()
    failure: Sync | None = None
    output: list[str] = []
    for command in steps:
        try:
            done = _run(command, root, env, timeout_s)
        except process.ToolMissing as ex:
            return Environment(
                (), Sync("skipped", reason=str(ex)), _inputs(root, plan, shared)
            )
        except process.ToolTimeout:
            failure = Sync(
                "failed",
                plan.tool,
                command,
                time.monotonic() - started,
                reason=f"the installer did not finish within {timeout_s:g} s",
            )
            break
        except Exception as ex:
            failure = Sync(
                "failed", plan.tool, command, time.monotonic() - started, reason=str(ex)
            )
            break
        output.extend((done.out, done.err))
        if not done.is_ok:
            failure = Sync(
                "failed",
                plan.tool,
                command,
                time.monotonic() - started,
                done.exit_code,
                _diagnostics(done),
            )
            break
        if not python:
            printed = [line.strip() for line in done.out.splitlines() if line.strip()]
            python = (printed[-1],) if printed else ()
            if not _is_usable(python):
                failure = Sync(
                    "failed",
                    plan.tool,
                    command,
                    time.monotonic() - started,
                    done.exit_code,
                    _diagnostics(done),
                    "the installer named no interpreter",
                )
                break
        finished += 1
    if failure is not None:
        if not any(output) and failure.reason:
            output.append(failure.reason + "\n")
        failure = dataclasses.replace(failure, log=_save_failure_log(output))
        if makes_venv and finished:
            # `uv venv --clear` made this `.venv`, also where a broken one was.
            leftovers.append(root / ".venv")
            failure = dataclasses.replace(failure, note=REMADE if is_remade else "")
        for path in leftovers:
            # An empty `.venv` holds none of the project's packages, and a lock
            # makes a uv project of this one. The next start must see neither.
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        return Environment(existing, failure)
    notes = [REMADE] if is_remade else []
    changes = _changes(before, _versions(python, shared)) if upgrade else ()
    if upgrade and not changes and _is_offline(env):
        notes.append(OFFLINE)
    if not shared:
        notes.append(_add_pip(python, root, env, timeout_s))
    digest = _inputs(root, plan, shared)
    _write_stamp(
        stamp_file,
        {
            "version": STAMP_VERSION,
            "inputs": digest,
            "state": _state(python, shared),
            "python": list(python),
        },
    )
    return Environment(
        python,
        Sync(
            "upgraded" if upgrade else "installed",
            plan.tool,
            steps[-1],
            time.monotonic() - started,
            note="; ".join(note for note in notes if note),
            changes=changes,
        ),
        digest,
    )


@contextlib.contextmanager
def locked(cwd: str):
    """One start at a time for the project in `cwd`, in every process. Two
    installers at once make the same `.venv` twice, under each other."""
    path = Path(sync_stamps()) / f"{_digest([str(_root(cwd)), 'lock'])[:24]}.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "w", encoding="utf-8")
    except OSError:
        yield
        return
    with handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def activation(python: tuple[str, ...], path: str | None = None) -> dict[str, str]:
    """The variables that `uv run` sets for a virtualenv: its programs come
    first on `path`. Empty when `python` is not the interpreter of one."""
    if len(python) != 1 or not os.path.isabs(python[0]):
        # A bare `python3` comes from PATH and belongs to no virtualenv here.
        return {}
    home = Path(python[0]).parent.parent
    if not (home / "pyvenv.cfg").is_file():
        return {}
    after = os.environ.get("PATH", "") if path is None else path
    return {
        "VIRTUAL_ENV": str(home),
        "PATH": os.pathsep.join(part for part in (str(home / "bin"), after) if part),
    }


def describe(sync: Sync | None, *, is_running: bool = True) -> str:
    """One `sync` in plain words for the result of a start, or ""."""
    if sync is None or sync.state == "none":
        return ""
    if sync.state == "current":
        return "dependencies current"
    if sync.state in ("installed", "upgraded"):
        said = f"dependencies {sync.state} with {sync.tool} in {sync.seconds:.1f} s"
        if sync.state == "upgraded":
            shown = ", ".join(sync.changes[:CHANGES_SHOWN])
            more = len(sync.changes) - CHANGES_SHOWN
            if more > 0:
                shown += f" and {more} more"
            said = (
                f"{said}: {shown}"
                if shown
                else f"dependencies checked with {sync.tool} in {sync.seconds:.1f} s:"
                " no newer version was installed"
            )
        return f"{said}; {sync.note}" if sync.note else said
    if sync.state == "skipped":
        return f"dependencies NOT installed: {sync.reason}"
    return "\n".join(failure_lines(sync, is_running=is_running))


def failure_lines(sync: Sync, *, is_running: bool) -> list[str]:
    """Show the failure and a few diagnostics; keep full output out of the result."""
    log = f"Installer log: {sync.log}" if sync.log else "Installer log unavailable."
    why = (
        f"the installer exited with status {sync.exit_code}"
        if sync.exit_code is not None
        else ""
    )
    reason = _short(sync.reason.splitlines()[0], 110) if sync.reason else ""
    summary = "; ".join(filter(None, (why, reason)))
    lines = [f"dependency installation FAILED after {sync.seconds:.1f} s: {summary}."]
    if is_running:
        lines.append("The REPL uses the existing environment.")
    lines.append(log)
    for diagnostic in sync.output:
        room = min(110, FAILURE_LIMIT - sum(len(line) + 1 for line in lines))
        if room < 20:
            break
        lines.append(_short(diagnostic, room))
    return lines


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
