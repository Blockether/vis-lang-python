"""Formatting and lint through ruff.

The project's own ruff wins: a checkout that pins a ruff version formats the
same way here as it does in its CI. When the project has none, the copy this
extension installs is used, so the tools still work in a bare directory.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from vis_lang_interface import Diagnostic, FormatResult, LintResult, process

SUFFIXES = (".py", ".pyi")
INSTALL_HINT = "install it with `pip install ruff`, or add it to the project."
# Ruff reports no severity. These families are real defects rather than style:
# E9 is a syntax error, F6/F7/F8 are broken or undefined code.
ERROR_PREFIXES = ("E9", "F6", "F7", "F8")


def ruff_path(root):
    """The ruff to run for `root`: the project's first, then this extension's.

    Raises:
        ToolMissing: No ruff is installed anywhere this extension can see.
    """
    for candidate in (".venv/bin/ruff", "venv/bin/ruff"):
        local = Path(root) / candidate
        if local.is_file():
            return str(local)
    found = shutil.which("ruff")
    if found:
        return found
    try:
        from ruff.__main__ import find_ruff_bin

        return str(find_ruff_bin())
    except (ImportError, FileNotFoundError):
        return process.tool_path("ruff", INSTALL_HINT)


def format_files(paths, root, *, is_written=False):
    """Report, and optionally apply, ruff's formatting.

    Args:
        paths: Files to format.
        root: Project directory ruff runs in, so it reads the project's config.
        is_written: Whether to rewrite the files that differ.

    Returns:
        A `FormatResult`. `changed` lists the files ruff would rewrite.

    Raises:
        ToolMissing: ruff is not installed.
        RuntimeError: ruff failed for a reason other than unformatted files.
    """
    names = [str(path) for path in paths]
    ruff = ruff_path(root)
    done = process.run([ruff, "format", "--check", "--quiet", *names], cwd=root)
    if done.exit_code not in (0, 1):
        raise RuntimeError(f"ruff format failed: {(done.err or done.out).strip()}")
    changed = tuple(
        line.split("Would reformat:", 1)[1].strip()
        for line in done.out.splitlines()
        if line.startswith("Would reformat:")
    )
    if changed and is_written:
        written = process.run([ruff, "format", "--quiet", *names], cwd=root)
        if written.exit_code != 0:
            raise RuntimeError(
                f"ruff format failed: {(written.err or written.out).strip()}"
            )
    unchanged = tuple(name for name in names if name not in changed)
    return FormatResult(
        "python", changed, unchanged, is_written=is_written and bool(changed)
    )


def format_source(source, root):
    """`source` as ruff would format it.

    Raises:
        ToolMissing: ruff is not installed.
        ValueError: The source does not parse.
    """
    done = process.run(
        [ruff_path(root), "format", "--quiet", "-"], cwd=root, stdin=source
    )
    if done.exit_code != 0:
        raise ValueError(f"ruff could not format the source: {done.err.strip()}")
    return FormatResult("python", (), (), source=done.out)


def check_files(paths, root, *, is_fixed=False):
    """Every ruff finding for `paths`.

    Args:
        paths: Files to lint.
        root: Project directory ruff runs in.
        is_fixed: Whether to apply ruff's safe fixes first.

    Returns:
        A `LintResult` whose diagnostics carry ruff's own rule codes.

    Raises:
        ToolMissing: ruff is not installed.
        RuntimeError: ruff could not run.
    """
    names = [str(path) for path in paths]
    ruff = ruff_path(root)
    if is_fixed:
        process.run([ruff, "check", "--fix-only", "--quiet", *names], cwd=root)
    done = process.run(
        [ruff, "check", "--output-format", "json", "--quiet", *names], cwd=root
    )
    if done.exit_code not in (0, 1):
        raise RuntimeError(f"ruff check failed: {(done.err or done.out).strip()}")
    return LintResult.of("python", diagnostics_of(done.out, root), len(names))


def diagnostics_of(report, root=""):
    """Ruff's JSON report as contract diagnostics.

    Raises:
        ValueError: The report is not the JSON ruff documents.
    """
    if not report.strip():
        return ()
    try:
        rows = json.loads(report)
    except ValueError as exc:
        raise ValueError(f"ruff did not answer with JSON: {report[:200]}") from exc
    findings = []
    for row in rows:
        code = row.get("code") or ""
        location = row.get("location") or {}
        path = row.get("filename") or ""
        if root and path.startswith(str(root)):
            path = str(Path(path).relative_to(root))
        level = "error" if code.startswith(ERROR_PREFIXES) or not code else "warning"
        findings.append(
            Diagnostic(
                path,
                int(location.get("row") or 0),
                int(location.get("column") or 0),
                level,
                str(row.get("message") or ""),
                code,
            )
        )
    return tuple(findings)
