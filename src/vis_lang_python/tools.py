"""The Python tools this extension exports.

Ordinary Python: every method takes plain arguments and returns a contract
result from `vis_lang_interface`, so the same calls work in a script, in a test
and from Vis.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from vis_lang_interface import (
    FormatResult,
    LintResult,
    ReplResult,
    ReplSession,
    TestResult,
    process,
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

SESSION_ROOT_TIMEOUT_S = 30
"""Seconds the one question about the session's own directory may take."""

_SESSION_ROOT: dict[str, str] = {}
"""Where the session lives, once the host has been asked."""


def _asked_root():
    """Where the host's shell runs, or an empty string when it cannot say.

    Every command this extension starts goes through that shell, and the host
    runs it in the session's workspace, so asking it once is asking the session
    where it lives. A refusal or a timeout is nothing to fail a format or a test
    run over: the caller keeps the process directory it used before.
    """
    try:
        done = process.run([process.tool_path("pwd")], timeout_s=SESSION_ROOT_TIMEOUT_S)
    except Exception:  # Any failure here only means the host cannot say.
        return ""
    answered = done.out.strip().splitlines()
    return answered[-1].strip() if done.is_ok and answered else ""


def session_root():
    """The directory a relative path in a call means: the session's own.

    Vis runs the engine, and every extension inside it, from its installation
    directory, so `Path.cwd()` here is that checkout and not the project the
    session is about. `cwd="."` therefore named `~/.vis/install/src`, and a
    project elsewhere answered with a missing pytest or a file that was not
    there (Blockether/vis#280). The host's shell knows where the session is, and
    its answer holds as long as this process does. Outside Vis nothing hosts a
    session, and the process directory is already the answer.
    """
    if not process.is_hosted():
        return str(Path.cwd())
    if not _SESSION_ROOT.get("root"):
        answered = _asked_root()
        if answered:
            _SESSION_ROOT["root"] = answered
    return _SESSION_ROOT.get("root") or str(Path.cwd())


def _absolute(path):
    """`path` as the caller means it: a relative name belongs to the session."""
    named = Path(path).expanduser()
    return named if named.is_absolute() else Path(session_root()) / named


def _root(cwd, paths=()):
    """The project directory a call runs in."""
    start = cwd or (paths[0] if paths else ".")
    return str(project_root(_absolute(start), MARKERS))


def _in_root(root, paths):
    """`paths` as the caller means them: a relative name belongs to `root`.

    ruff runs in `root`, so it already reads relative names that way; the files
    are also read and rewritten here, where the process directory is Vis' own.
    """
    return tuple(
        str(one) if Path(one).is_absolute() else str(Path(root) / one) for one in paths
    )


def _session(language, answer):
    """A `repl` answer as a `ReplSession`."""
    return ReplSession(
        language,
        repl.abbreviate_home(answer["cwd"]),
        answer["cwd"],
        tuple(answer.get("cmd") or ()),
        answer.get("status") == "up",
        answer.get("result", ""),
    )


class PythonTools:
    """Format, lint and test Python, and evaluate in a project REPL."""

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
        root = _root(cwd, tuple(paths))
        if source:
            return ruff_tool.format_source(source, root)
        files = source_files(_in_root(root, paths), ruff_tool.SUFFIXES)
        if not files:
            raise ValueError("no Python file in the given paths")
        return ruff_tool.format_files(files, root, is_written=is_written)

    def lint_code(
        self,
        paths: Annotated[list[str], "Files or directories to lint."],
        *,
        cwd: Annotated[str, "Project directory; inferred from paths when empty."] = "",
        is_fixed: Annotated[bool, "Apply ruff's safe fixes first."] = False,
    ) -> LintResult:
        """Lint Python with ruff and report every finding it located.

        Findings keep ruff's own rule codes. Syntax errors and undefined names
        are reported as errors, every other rule as a warning. Raises
        ToolMissing when no ruff is installed.
        """
        root = _root(cwd, tuple(paths))
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
            root=_root(cwd, tuple(paths)),
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
        answer = repl.start({"cwd": str(_absolute(cwd))})
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
        return _session("python", repl.status({"cwd": str(_absolute(cwd))}))

    def repl_stop(
        self,
        cwd: Annotated[str, "Project directory the interpreter runs in."] = "",
    ) -> ReplSession:
        """Stop this directory's interpreter. Safe when none is running."""
        return _session("python", repl.stop({"cwd": str(_absolute(cwd))}))

    def repl_eval(
        self,
        code: Annotated[str, "Python source to evaluate."],
        *,
        cwd: Annotated[str, "Project directory the interpreter runs in."] = "",
        timeout_ms: Annotated[int, "Milliseconds to wait for the answer."] = 30000,
    ) -> ReplResult:
        """Evaluate code in the live interpreter, keeping globals between calls.

        The value of a trailing expression comes back as its repr, with anything
        the code printed. Start the REPL first: evaluating without one raises
        ReplError, as does an evaluation that outlives timeout_ms.
        """
        directory = str(_absolute(cwd))
        answer = repl.evaluate(
            {"code": code, "cwd": directory, "timeout_ms": timeout_ms}
        )
        return ReplResult(
            "python",
            repl.abbreviate_home(str(Path(directory).expanduser().resolve())),
            answer.get("value") or "",
            (answer.get("out") or "") + (answer.get("err") or ""),
            answer.get("exc") or "",
            0,
            True,
        )
