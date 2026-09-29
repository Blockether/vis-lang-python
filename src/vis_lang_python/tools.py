"""The Python tools this extension exports.

Ordinary Python: every method takes plain arguments and returns a contract
result from `vis_lang_interface`. The same calls work in a script, in a test and
from Vis.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

from vis_lang_interface import (
    FormatResult,
    LintResult,
    ReplResult,
    ReplSession,
    TestResult,
    project_root,
    source_files,
)

from vis_lang_python import pytest_tool, repl, ruff_tool

MARKERS = (
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "uv.lock",
    "requirements.txt",
    ".git",
)


def _absolute(path, workspace_root=Path.cwd):
    """Resolve a relative name in the current working copy, never a sampled root."""
    named = Path(path).expanduser()
    return named if named.is_absolute() else Path(workspace_root()) / named


def _root(cwd, paths=(), workspace_root=Path.cwd):
    """The nearest Python project in the working copy chosen for this call."""
    start = cwd or (paths[0] if paths else ".")
    return str(project_root(_absolute(start, workspace_root), MARKERS))


def _in_root(root, paths):
    """`paths` as the caller means them: a relative name belongs to `root`.

    ruff runs in `root`, so it already reads relative names that way. This
    process also reads and rewrites the files, but its working directory belongs
    to Vis.
    """
    return tuple(
        str(one) if Path(one).is_absolute() else str(Path(root) / one) for one in paths
    )


def _session(language, answer):
    """A `repl` answer as a `ReplSession`, its detail in plain words."""
    is_running = answer.get("status") == "up"
    happened = answer.get("result", "")
    if happened == "status":
        happened = "running" if is_running else "not running"
    return ReplSession(
        language,
        repl.abbreviate_home(answer["cwd"]),
        answer["cwd"],
        tuple(answer.get("cmd") or ()),
        is_running,
        happened.replace("-", " "),
    )


def _pretty(code, root):
    """`code` as ruff formats it in `root`, or as given when ruff cannot format it.

    Only the evaluation's presentation uses it, so a missing ruff or code that
    does not parse must never fail the evaluation itself.
    """
    try:
        return ruff_tool.format_source(code, root).source.rstrip()
    except Exception:
        return code.rstrip()


class PythonTools:
    """Format, lint and test Python, and evaluate in a project REPL."""

    def __init__(self, *, workspace_root=Path.cwd):
        """Keep a live root provider. Hosted tools receive the SDK function."""
        self._workspace_root = workspace_root

    def _absolute(self, path):
        return _absolute(path, self._workspace_root)

    def _root(self, cwd, paths=()):
        return _root(cwd, paths, self._workspace_root)

    def format_code(
        self,
        paths: Annotated[list[str], "Files or directories to format."] = (),
        *,
        source: Annotated[str, "Format this text instead of files."] = "",
        cwd: Annotated[str, "Project directory; inferred from paths when empty."] = "",
        is_written: Annotated[bool, "Rewrite the files that differ."] = False,
    ) -> FormatResult:
        """Format Python with the project's ruff, or the bundled one.

        With source, the formatted text comes back and nothing is written. With
        paths, the result lists the files that differ, and is_written rewrites
        them. Raises ToolMissing when no ruff is installed and ValueError when
        neither source nor paths are given.
        """
        root = self._root(cwd, tuple(paths))
        if source:
            return ruff_tool.format_source(source, root)
        files = source_files(_in_root(root, paths), ruff_tool.SUFFIXES)
        if not files:
            raise ValueError("no Python file in the given paths")
        return ruff_tool.format_files(files, root, is_written=is_written)

    def lint_code(
        self,
        paths: Annotated[list[str], "Files or directories to lint."] = (),
        *,
        source: Annotated[str, "Lint this text instead of files."] = "",
        cwd: Annotated[str, "Project directory; inferred from paths when empty."] = "",
        is_fixed: Annotated[bool, "Apply ruff's safe fixes first."] = False,
    ) -> LintResult:
        """Lint Python with ruff and report every finding it located.

        Findings keep ruff's own rule codes. Syntax errors and undefined names
        are reported as errors, and every other rule as a warning. With source,
        that text is linted under the project's settings, and its findings are
        reported against `<stdin>`. Nothing is written then, so is_fixed needs
        paths. Raises ToolMissing when no ruff is installed, and ValueError when
        neither source nor paths are given.
        """
        root = self._root(cwd, tuple(paths))
        if source:
            if is_fixed:
                raise ValueError("is_fixed rewrites files; pass paths to apply fixes")
            return ruff_tool.check_source(source, root)
        files = source_files(_in_root(root, paths), ruff_tool.SUFFIXES)
        if not files:
            raise ValueError("no Python file in the given paths")
        return ruff_tool.check_files(files, root, is_fixed=is_fixed)

    def run_tests(
        self,
        paths: Annotated[
            list[str], "Test files or directories; pytest discovers when empty."
        ] = (),
        *,
        cwd: Annotated[str, "Project directory; inferred from paths when empty."] = "",
        keyword: Annotated[str, "Value for pytest's -k selection."] = "",
        timeout_s: Annotated[int, "Seconds before the run is abandoned."] = 900,
    ) -> TestResult:
        """Run pytest under the project's own interpreter.

        Counts and failures come from the JUnit report pytest writes. Raises
        ToolMissing when the project has no pytest and ToolTimeout when the run
        outlives timeout_s.
        """
        return pytest_tool.run(
            tuple(paths),
            root=self._root(cwd, tuple(paths)),
            keyword=keyword,
            timeout_s=timeout_s,
        )

    def repl_start(
        self,
        cwd: Annotated[str, "Project directory to run the interpreter in."] = "",
    ) -> ReplSession:
        """Start a persistent interpreter for this directory, or keep the live one.

        The project's own interpreter is used: uv, Poetry, a local virtualenv or
        python3, in that order. A live REPL is never replaced, because its
        globals are the work. Raises RuntimeError when the interpreter fails its
        startup handshake.
        """
        answer = repl.start({"cwd": str(self._absolute(cwd))})
        if answer.get("result") == "failed":
            detail = " ".join(answer.get("log_tail") or ())
            raise RuntimeError(
                f"{answer.get('message', 'REPL failed')} {detail}".strip()
            )
        return _session("python", answer)

    def repl_status(
        self,
        cwd: Annotated[str, "Project directory the interpreter runs in."] = "",
    ) -> ReplSession:
        """Whether this directory has a live interpreter, and what launched it."""
        return _session("python", repl.status({"cwd": str(self._absolute(cwd))}))

    def repl_stop(
        self,
        cwd: Annotated[str, "Project directory the interpreter runs in."] = "",
    ) -> ReplSession:
        """Stop this directory's interpreter. Safe when none is running."""
        return _session("python", repl.stop({"cwd": str(self._absolute(cwd))}))

    def repl_eval(
        self,
        code: Annotated[str, "Python source to evaluate."],
        *,
        cwd: Annotated[str, "Project directory the interpreter runs in."] = "",
        timeout_ms: Annotated[int, "Milliseconds to wait for the answer."] = 30000,
    ) -> ReplResult:
        """Evaluate code in the live interpreter, keeping globals between calls.

        The value of a trailing expression comes back as its repr, laid out by
        pprint when it is long. The result also carries anything the code
        printed, and the code as ruff formats it. Start the REPL first.
        Evaluating without one raises ReplError, and so does an evaluation that
        takes longer than timeout_ms.
        """
        directory = str(self._absolute(cwd))
        started = time.monotonic()
        answer = repl.evaluate(
            {"code": code, "cwd": directory, "timeout_ms": timeout_ms}
        )
        return ReplResult(
            "python",
            repl.abbreviate_home(str(Path(directory).expanduser().resolve())),
            answer.get("value") or "",
            (answer.get("out") or "") + (answer.get("err") or ""),
            answer.get("exc") or "",
            round((time.monotonic() - started) * 1000),
            True,
            code=_pretty(code, directory),
        )
