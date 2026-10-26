"""Running the project's tests with pytest.

pytest runs under the project's own interpreter: uv, Poetry, a `.venv` or
`python3`, or `vis-agent python --shared` when the project's
`python.environment` is `shared`. It is chosen the same way the REPL chooses
one. So the tests import the project's dependencies, not the dependencies of
this extension. Counts and failures come from the JUnit XML that pytest
writes, never from its terminal output.

With coverage, pytest runs under the project's coverage.py with branch
measurement. Its JSON report gives each file's covered lines and branches, and
the gaps: the lines and the branches that never ran.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path, PurePosixPath

from vis_lang_interface import (
    CoverageGap,
    CoverageResult,
    FileCoverage,
    TestFailure,
    TestResult,
    process,
)

from vis_lang_python.repl import detect_command, environment_hint

OUTPUT_TAIL = 4000
INSTALL_HINT = "add pytest to the project, for example `uv add --dev pytest`."
COVERAGE_HINT = "add coverage to the project, for example `uv add --dev coverage`."
COVERAGE_REPORT = "coverage.json"
DETAIL_LIMIT = 100


def run(
    paths=(),
    *,
    root,
    keyword="",
    max_failures=0,
    timeout_s=900,
    shared=False,
    coverage=False,
):
    """Run pytest in `root` and read its JUnit report.

    Args:
        paths: Test files or directories. Empty means pytest's own discovery.
        root: Project directory to run in.
        keyword: Value for pytest's `-k` selection, or an empty string.
        max_failures: Stop after this many failures (`--maxfail`). 0 runs every test.
        timeout_s: Seconds before the run is abandoned.
        shared: Run with `vis-agent python --shared`, not the project environment.
        coverage: Run under coverage.py with branches, and write `coverage.json`
            in `root`.

    Returns:
        A `TestResult` with one entry per failing test, and its coverage when
        measured. A project without coverage.py gets the tests and a note.

    Raises:
        ToolMissing: The project has no pytest.
        ToolTimeout: The run outlived `timeout_s`.
        RuntimeError: pytest could not start or wrote no report.
        ValueError: max_failures is negative.
    """
    if max_failures < 0:
        raise ValueError("max_failures must be 0 or more")
    with tempfile.TemporaryDirectory() as workspace:
        report = Path(workspace) / "report.xml"
        python = detect_command(str(root), shared=shared)
        runner = ["-m", "coverage", "run", "--branch"] if coverage else []
        command = [
            *python,
            *runner,
            "-m",
            "pytest",
            *[str(path) for path in paths],
            "-q",
            "-p",
            "no:cacheprovider",
            # xunit1 keeps the file and line of each case; xunit2 drops both.
            "-o",
            "junit_family=xunit1",
            f"--junit-xml={report}",
        ]
        if keyword:
            command += ["-k", keyword]
        if max_failures:
            command.append(f"--maxfail={max_failures}")
        # The coverage data stays in the private directory; only the JSON report
        # goes to the project, where graphify reads it.
        env = (
            {"COVERAGE_FILE": str(Path(workspace) / ".coverage")} if coverage else None
        )
        # The JUnit report is written outside the project, so the run needs that directory.
        done = process.run(
            command, cwd=root, timeout_s=timeout_s, env=env, read_write=(workspace,)
        )
        output = (done.out + done.err)[-OUTPUT_TAIL:]
        if not report.is_file():
            if coverage and "No module named coverage" in output:
                result = run(
                    paths,
                    root=root,
                    keyword=keyword,
                    max_failures=max_failures,
                    timeout_s=timeout_s,
                    shared=shared,
                )
                why = f"coverage.py is not installed. {COVERAGE_HINT}"
                return dataclasses.replace(result, output=_noted(result.output, why))
            if "No module named pytest" in output:
                raise process.ToolMissing(f"pytest is not installed. {INSTALL_HINT}")
            hint = environment_hint(output)
            raise RuntimeError(
                f"pytest wrote no report: {output.strip()[-1000:]}"
                + (f"\n{hint}" if hint else "")
            )
        result = result_of(report.read_text(encoding="utf-8"), output, done.duration_ms)
        if not coverage:
            return result
        target = Path(root) / COVERAGE_REPORT
        written = process.run(
            [*python, "-m", "coverage", "json", "-q", "-o", str(target)],
            cwd=root,
            timeout_s=timeout_s,
            env=env,
            read_write=(workspace,),
        )
        if not target.is_file() or written.exit_code:
            why = (written.out + written.err).strip()[-1000:] or "no report"
            return dataclasses.replace(result, output=_noted(result.output, why))
        measured = coverage_of(json.loads(target.read_text(encoding="utf-8")), root)
        return dataclasses.replace(
            result, coverage=dataclasses.replace(measured, report=str(target))
        )


def _noted(output, why):
    """`output` with the reason that the run measured no coverage."""
    note = f"No coverage: {why}"
    return f"{output}\n\n{note}" if output else note


def _is_test_file(path):
    """Whether `path` is a test module, which coverage results leave out."""
    file = PurePosixPath(str(path).replace("\\", "/"))
    return (
        file.name == "conftest.py"
        or file.name.startswith("test_")
        or file.name.endswith("_test.py")
        or bool({"test", "tests"} & set(file.parts[:-1]))
    )


def _source(root, path):
    """The lines of a measured file, or none when it cannot be read."""
    try:
        return (Path(root) / path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def _text(lines, line):
    """Line `line` of a file, stripped and clipped, or an empty string."""
    text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
    if len(text) > DETAIL_LIMIT:
        return text[: DETAIL_LIMIT - 3] + "..."
    return text


def _line_gaps(info, lines):
    """Each run of statements that never ran, as one gap."""
    missing = set(info.get("missing_lines") or ())
    statements = sorted(set(info.get("executed_lines") or ()) | missing)
    gaps, current = [], []
    for line in [*statements, None]:
        if line in missing:
            current.append(line)
            continue
        if current:
            gaps.append(
                CoverageGap(current[0], current[-1], "line", _text(lines, current[0]))
            )
            current = []
    return gaps


def _branch_gaps(info, lines):
    """Each branch that never ran from a line that did, as `12 -> 14: source`."""
    missing = set(info.get("missing_lines") or ())
    gaps = []
    for start, end in info.get("missing_branches") or ():
        if start in missing:
            continue
        # coverage.py marks a jump out of the function with a negative line.
        target = "exit" if end < 0 else str(end)
        detail = f"{start} -> {target}: {_text(lines, start)}"
        gaps.append(CoverageGap(start, start, "branch", detail))
    return gaps


def coverage_of(data, root):
    """A coverage.py JSON report as a `CoverageResult`, without test modules."""
    files = []
    for path, info in (data.get("files") or {}).items():
        name = Path(path)
        if name.is_absolute():
            try:
                name = name.relative_to(Path(root).resolve())
            except ValueError:
                pass
        if _is_test_file(name.as_posix()):
            continue
        summary = info.get("summary") or {}
        lines = _source(root, path)
        gaps = sorted(
            [*_line_gaps(info, lines), *_branch_gaps(info, lines)],
            key=lambda gap: (gap.line, gap.kind != "line"),
        )
        files.append(
            FileCoverage(
                name.as_posix(),
                int(summary.get("covered_lines") or 0),
                int(summary.get("num_statements") or 0),
                branches_covered=int(summary.get("covered_branches") or 0),
                branches_total=int(summary.get("num_branches") or 0),
                gaps=tuple(gaps),
            )
        )
    return CoverageResult.of("python", "lines", files)


def _test_name(case):
    """A case's name under its classes, nested names joined with ' › '.

    pytest writes the classname as the dotted module path, followed by any
    classes. When the file is known, the part after the module is the chain of
    classes.
    """
    name = case.get("name") or ""
    classname = case.get("classname") or ""
    path = case.get("file") or ""
    module = str(PurePosixPath(path).with_suffix("")).replace("/", ".") if path else ""
    if module and classname.startswith(module + "."):
        return " › ".join([*classname[len(module) + 1 :].split("."), name])
    return name


def _line(case):
    """The 1-based line of a case, or 0 when the report has none."""
    line = case.get("line")
    return int(line) + 1 if line else 0


def result_of(report, output="", duration_ms=0):
    """The JUnit XML pytest wrote, as a `TestResult`.

    Raises:
        ValueError: The report is not JUnit XML.
    """
    try:
        root = ElementTree.fromstring(report)
    except ElementTree.ParseError as exc:
        raise ValueError("pytest did not write a JUnit report") from exc
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    total = failed = skipped = 0
    failures = []
    for suite in suites:
        total += int(suite.get("tests") or 0)
        failed += int(suite.get("failures") or 0) + int(suite.get("errors") or 0)
        skipped += int(suite.get("skipped") or 0)
        for case in suite.findall("testcase"):
            for problem in [*case.findall("failure"), *case.findall("error")]:
                failures.append(
                    TestFailure(
                        _test_name(case),
                        case.get("file") or "",
                        _line(case),
                        (problem.get("message") or problem.text or "").strip(),
                    )
                )
    return TestResult.of(
        "python",
        total=total,
        passed=max(total - failed - skipped, 0),
        failed=failed,
        skipped=skipped,
        duration_ms=duration_ms,
        failures=failures,
        output=output.strip(),
    )
