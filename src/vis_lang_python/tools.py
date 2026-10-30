"""The Python tools this extension exports.

Ordinary Python: every method takes plain arguments and returns a contract
result from `vis_lang_interface`. The same calls work in a script, in a test and
from Vis.
"""

from __future__ import annotations

import ast
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

from vis_lang_interface import (
    PATH_ALIASES,
    Diagnostic,
    FormatResult,
    LintResult,
    ReplResult,
    ReplSession,
    SyntaxResult,
    TestResult,
    aliased,
    project_root,
    source_files,
    string_list,
)

from vis_lang_python import pytest_tool, repl, ruff_tool
from vis_lang_python.repair import repair_source

MARKERS = (
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "uv.lock",
    "requirements.txt",
    ".git",
)

SYNTAX_SUFFIXES = (".py", ".pyi")


def _absolute(path, workspace_root=Path.cwd):
    """Resolve a relative name in the current working copy, never a sampled root."""
    named = Path(path).expanduser()
    return named if named.is_absolute() else Path(workspace_root()) / named


def _root(cwd, paths=(), workspace_root=Path.cwd):
    """The nearest Python project in the working copy chosen for this call."""
    start = cwd or (paths[0] if paths else ".")
    return str(project_root(_absolute(start, workspace_root), MARKERS))


def _check_syntax(sources, root):
    """Check source locally with Python's parser and compiler, without execution.

    The extension's interpreter decides this fast edit check. Project lint and
    tests still run with the project's own tools. No subprocess is started.
    """
    diagnostics = []
    for path, text in sorted(sources.items()):
        try:
            tree = ast.parse(text, filename=str(path))
            compile(tree, str(path), "exec", dont_inherit=True)
        except SyntaxError as error:
            diagnostics.append(
                Diagnostic(
                    str(path), error.lineno or 0, error.offset or 0, "error", error.msg
                )
            )
    return SyntaxResult.of("python", diagnostics, files=len(sources))


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


def _parses(text):
    """True when the local parser and compiler accept `text`, as in the edit check."""
    return _check_syntax({"<repl>": text}, "").is_clean


def _unparsed(syntax, exc):
    """The error for code that did not parse: where it stops, and what to do next."""
    return (
        f"Line {syntax.get('line')}, column {syntax.get('column')}: "
        f"{syntax.get('message')}. The code was not evaluated.\n"
        "No safe repair exists. Fix the syntax, then evaluate again.\n"
    ) + (exc or "")


def _start_failure(answer):
    """The error text of a failed REPL start: the cause, the command and the output."""
    lines = [str(answer.get("message") or "REPL failed")]
    command = answer.get("cmd")
    if command:
        lines.append(f"Command: {' '.join(str(part) for part in command)}")
    tail = answer.get("log_tail")
    if tail:
        lines.append("Last output of the interpreter:")
        lines.extend(tail)
        return "\n".join(lines)
    probe = answer.get("probe")
    if not probe:
        return "\n".join(lines)
    lines.append("The interpreter wrote nothing to its log.")
    said = list(probe.get("output") or ())
    if "error" in probe:
        lines.append(f"A check run of the same interpreter failed: {probe['error']}")
    elif probe.get("exit") == 0:
        lines.append(
            "A check run of the same interpreter works, so only the kept REPL failed."
        )
    else:
        code = probe.get("exit")
        meaning = repl.exit_meaning(code)
        lines.append(
            f"A check run of the same interpreter exited with status {code}"
            + (f": {meaning}." if meaning else ".")
        )
    if said:
        lines.append("Check run output:")
        lines.extend(said)
    return "\n".join(lines)


class PythonTools:
    """Format, lint and test Python, and evaluate in a project REPL."""

    def __init__(
        self, *, workspace_root=Path.cwd, python_environment=lambda: "project"
    ):
        """Keep live providers. Hosted tools receive the SDK functions.

        `python_environment` answers `project` or `shared`, the project's
        `python.environment`. With `shared`, tests and the REPL run with
        `vis-agent python --shared`.
        """
        self._workspace_root = workspace_root
        self._python_environment = python_environment

    def _shared(self):
        return self._python_environment() == "shared"

    def _absolute(self, path):
        return _absolute(path, self._workspace_root)

    def _root(self, cwd, paths=()):
        return _root(cwd, paths, self._workspace_root)

    @aliased(**PATH_ALIASES, code="source", write="is_written", written="is_written")
    def format_code(
        self,
        paths: Annotated[Sequence[str], "Files or directories to format."] = (),
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
        paths = string_list(paths)
        root = self._root(cwd, paths)
        if source:
            return ruff_tool.format_source(source, root)
        files = source_files(_in_root(root, paths), ruff_tool.SUFFIXES)
        if not files:
            raise ValueError("no Python file in the given paths")
        return ruff_tool.format_files(files, root, is_written=is_written)

    @aliased(**PATH_ALIASES, code="source", fix="is_fixed", fixed="is_fixed")
    def lint_code(
        self,
        paths: Annotated[Sequence[str], "Files or directories to lint."] = (),
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
        paths = string_list(paths)
        root = self._root(cwd, paths)
        if source:
            if is_fixed:
                raise ValueError("is_fixed rewrites files; pass paths to apply fixes")
            return ruff_tool.check_source(source, root)
        files = source_files(_in_root(root, paths), ruff_tool.SUFFIXES)
        if not files:
            raise ValueError("no Python file in the given paths")
        return ruff_tool.check_files(files, root, is_fixed=is_fixed)

    @aliased(
        **PATH_ALIASES,
        k="keyword",
        keywords="keyword",
        maxfail="max_failures",
        max_fail="max_failures",
    )
    def run_tests(
        self,
        paths: Annotated[
            Sequence[str], "Test files or directories; pytest discovers when empty."
        ] = (),
        *,
        cwd: Annotated[str, "Project directory; inferred from paths when empty."] = "",
        keyword: Annotated[str, "Value for pytest's -k selection."] = "",
        max_failures: Annotated[
            int, "Stop after this many failures (pytest --maxfail); 0 runs every test."
        ] = 0,
        coverage: Annotated[
            bool, "Also measure coverage: lines, branches and gaps (coverage.py)."
        ] = False,
        timeout_s: Annotated[int, "Seconds before the run is abandoned."] = 900,
    ) -> TestResult:
        """Run pytest under the project's own interpreter, or the shared one.

        Counts and failures come from the JUnit report pytest writes. With
        coverage, pytest runs under the project's coverage.py with branches. The
        result's coverage gives each file's covered lines and branches, and its
        gaps: the lines and branches that never ran. The run writes
        `coverage.json`, which `graphify crap` reads. Without coverage.py, the
        tests still run and the output says why coverage is missing. Raises
        ToolMissing when the project has no pytest and ToolTimeout when the run
        outlives timeout_s.
        """
        paths = string_list(paths)
        return pytest_tool.run(
            paths,
            root=self._root(cwd, paths),
            keyword=keyword,
            max_failures=max_failures,
            timeout_s=timeout_s,
            shared=self._shared(),
            coverage=coverage,
        )

    def repl_start(
        self,
        cwd: Annotated[str, "Project directory to run the interpreter in."] = "",
    ) -> ReplSession:
        """Start a persistent interpreter for this directory, or keep the live one.

        The project's own interpreter is used: uv, Poetry, a local virtualenv or
        python3, in that order. When the project's python.environment is shared,
        vis-agent python --shared is used. A live REPL is never replaced, because its
        globals are the work. Raises RuntimeError when the interpreter fails its
        startup handshake.
        """
        answer = repl.start({"cwd": str(self._absolute(cwd)), "shared": self._shared()})
        if answer.get("result") == "failed":
            raise RuntimeError(_start_failure(answer))
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
        takes longer than timeout_ms. Code that does not parse runs nothing. A
        safe repair, such as a missing closer, is evaluated instead and listed
        in `repairs`. Without one, the error says where the code stops parsing.
        """
        directory = str(self._absolute(cwd))
        started = time.monotonic()
        request = {"code": code, "cwd": directory, "timeout_ms": timeout_ms}
        answer = repl.evaluate(request)
        repairs = ()
        syntax = answer.get("syntax")
        repaired = syntax and repair_source(code, parses_clean=_parses)
        if repaired:
            retry = repl.evaluate(dict(request, code=repaired.source))
            if not retry.get("syntax"):
                code, answer, repairs = repaired.source, retry, repaired.notes
        if syntax and not repairs:
            answer = dict(answer, exc=_unparsed(syntax, answer.get("exc")))
        return ReplResult(
            "python",
            repl.abbreviate_home(str(Path(directory).expanduser().resolve())),
            answer.get("value") or "",
            (answer.get("out") or "") + (answer.get("err") or ""),
            answer.get("exc") or "",
            round((time.monotonic() - started) * 1000),
            True,
            code=_pretty(code, directory),
            repairs=repairs,
        )
