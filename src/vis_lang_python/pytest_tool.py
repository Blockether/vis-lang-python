"""Running the project's tests with pytest.

pytest runs under the project's own interpreter — uv, Poetry, a `.venv` or
`python3`, chosen the same way the REPL chooses one — so the tests import the
project's dependencies and not this extension's. Counts and failures are read
from the JUnit XML pytest writes, never scraped from its terminal output.
"""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from vis_lang_interface import TestFailure, TestResult, process

from vis_lang_python.caches import granted_paths
from vis_lang_python.repl import detect_command

OUTPUT_TAIL = 4000
INSTALL_HINT = "add pytest to the project, for example `uv add --dev pytest`."


def run(paths=(), *, root, keyword="", timeout_s=900):
    """Run pytest in `root` and read its JUnit report.

    Args:
        paths: Test files or directories; pytest's own discovery when empty.
        root: Project directory to run in.
        keyword: Value for pytest's `-k` selection, or an empty string.
        timeout_s: Seconds before the run is abandoned.

    Returns:
        A `TestResult` with one entry per failing test.

    Raises:
        ToolMissing: The project has no pytest.
        ToolTimeout: The run outlived `timeout_s`.
        RuntimeError: pytest could not start or wrote no report.
    """
    with tempfile.TemporaryDirectory() as workspace:
        report = Path(workspace) / "report.xml"
        command = [
            *detect_command(str(root)),
            "-m",
            "pytest",
            *[str(path) for path in paths],
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junit-xml={report}",
        ]
        if keyword:
            command += ["-k", keyword]
        done = process.run(
            command, cwd=root, timeout_s=timeout_s, read_write=granted_paths(workspace)
        )
        output = (done.out + done.err)[-OUTPUT_TAIL:]
        if not report.is_file():
            if "No module named pytest" in output:
                raise process.ToolMissing(f"pytest is not installed. {INSTALL_HINT}")
            raise RuntimeError(f"pytest wrote no report: {output.strip()[-1000:]}")
        return result_of(report.read_text(encoding="utf-8"), output, done.duration_ms)


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
                        case.get("name") or "",
                        case.get("file") or "",
                        int(case.get("line") or 0) + 1,
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
