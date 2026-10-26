"""Counts and failures are read from the report pytest writes, not its output."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from vis_lang_python import pytest_tool

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="3" time="0.4">
<testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py" line="3" time="0.1"/>
<testcase classname="tests.test_a" name="test_bad" file="tests/test_a.py" line="7" time="0.1">
<failure message="assert 1 == 2">E assert 1 == 2</failure></testcase>
<testcase classname="tests.test_a" name="test_later" file="tests/test_a.py" line="11" time="0.0">
<skipped message="not today"/></testcase>
</testsuite></testsuites>
"""


def test_counts_come_from_the_report():
    result = pytest_tool.result_of(REPORT, "1 failed, 1 passed", 400)
    assert (result.total, result.passed, result.failed, result.skipped) == (3, 1, 1, 1)
    assert result.is_passed is False
    assert result.duration_ms == 400


def test_failures_are_located():
    failure = pytest_tool.result_of(REPORT).failures[0]
    assert failure.test == "test_bad"
    assert failure.path == "tests/test_a.py"
    assert failure.line == 8
    assert "assert 1 == 2" in failure.message


def test_a_report_that_is_not_xml_is_refused():
    with pytest.raises(ValueError, match="JUnit"):
        pytest_tool.result_of("not xml at all")


def test_running_a_real_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    (tmp_path / "test_math.py").write_text(
        "def test_ok():\n    assert 1 == 1\n\n\ndef test_bad():\n    assert 1 == 2\n"
    )
    result = pytest_tool.run(root=str(tmp_path), timeout_s=120)
    assert (result.total, result.passed, result.failed) == (2, 1, 1)
    assert result.failures[0].test == "test_bad"
    assert result.is_passed is False


def test_selecting_tests_by_keyword(tmp_path):
    (tmp_path / "test_math.py").write_text(
        "def test_ok():\n    assert 1 == 1\n\n\ndef test_bad():\n    assert 1 == 2\n"
    )
    result = pytest_tool.run(root=str(tmp_path), keyword="ok", timeout_s=120)
    assert (result.total, result.passed, result.failed) == (1, 1, 0)
    assert result.is_passed is True


def test_a_run_stops_after_max_failures(tmp_path):
    (tmp_path / "test_math.py").write_text(
        "def test_bad_one():\n    assert 1 == 2\n\n\n"
        "def test_bad_two():\n    assert 1 == 3\n\n\n"
        "def test_ok():\n    assert 1 == 1\n"
    )
    result = pytest_tool.run(root=str(tmp_path), max_failures=1, timeout_s=120)
    assert (result.total, result.failed) == (1, 1)
    assert [failure.test for failure in result.failures] == ["test_bad_one"]


def test_a_negative_max_failures_is_refused(tmp_path):
    with pytest.raises(ValueError, match="max_failures"):
        pytest_tool.run(root=str(tmp_path), max_failures=-1, timeout_s=120)


def test_a_run_asks_only_for_the_directory_it_reports_into(tmp_path, monkeypatch):
    seen = {}

    def fake_run(command, *, cwd, timeout_s=300, stdin=None, env=None, read_write=()):
        seen["read_write"] = [str(path) for path in read_write]
        seen["report"] = command[-1].split("=", 1)[1]
        Path(seen["report"]).write_text(REPORT, encoding="utf-8")
        return SimpleNamespace(out="", err="", duration_ms=1, exit_code=0)

    monkeypatch.setattr(pytest_tool.process, "run", fake_run)
    pytest_tool.run(root=str(tmp_path))
    # The JUnit report is written outside the project, so the run is handed that
    # directory. The caches come from the grants that the extension declares.
    assert seen["read_write"] == [str(Path(seen["report"]).parent)]


def test_a_real_failure_is_named_with_its_class_and_located(tmp_path):
    (tmp_path / "test_math.py").write_text(
        "class TestSum:\n    def test_bad(self):\n        assert 1 + 1 == 3\n"
    )
    failure = pytest_tool.run(root=str(tmp_path), timeout_s=120).failures[0]
    assert (failure.test, failure.path, failure.line) == (
        "TestSum › test_bad",
        "test_math.py",
        2,
    )
    assert failure.message.splitlines()[0] == "assert (1 + 1) == 3"


NESTED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="0" skipped="0" tests="1">
<testcase classname="tests.test_a.TestOuter.TestInner" name="test_x" file="tests/test_a.py">
<error message="failed on setup with fixture 'db' not found">E fixture</error></testcase>
</testsuite></testsuites>
"""


def test_nested_classes_name_a_failure_and_a_missing_line_stays_unknown():
    failure = pytest_tool.result_of(NESTED).failures[0]
    assert (failure.test, failure.path, failure.line) == (
        "TestOuter › TestInner › test_x",
        "tests/test_a.py",
        0,
    )
    assert failure.message == "failed on setup with fixture 'db' not found"


# Regression, Blockether/vis#379: vis.yml set `python.environment: shared`, but
# pytest still ran through `uv run` and a broken `.venv` stopped it.
def test_a_shared_environment_runs_pytest_through_vis_agent(tmp_path, monkeypatch):
    (tmp_path / "uv.lock").write_text("")
    seen = {}

    def fake_run(command, **options):
        seen["command"] = command
        Path(command[-1].split("=", 1)[1]).write_text(REPORT, encoding="utf-8")
        return SimpleNamespace(out="", err="", duration_ms=1, exit_code=0)

    monkeypatch.setattr(pytest_tool.process, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    pytest_tool.run(root=str(tmp_path), shared=True)
    assert seen["command"][:5] == [
        "/bin/vis-agent",
        "python",
        "--shared",
        "-m",
        "pytest",
    ]


def test_a_broken_virtualenv_names_both_fixes(tmp_path, monkeypatch):
    # Blockether/vis#379: uv's own message reached the user without a next step.
    broken = (
        "error: Project virtual environment directory `.venv` cannot be used"
        " because it is not a valid Python environment (no Python executable was found)"
    )
    monkeypatch.setattr(
        pytest_tool.process,
        "run",
        lambda command, **options: SimpleNamespace(
            out="", err=broken, duration_ms=1, exit_code=2
        ),
    )
    with pytest.raises(RuntimeError) as failure:
        pytest_tool.run(root=str(tmp_path))
    assert "uv sync" in str(failure.value)
    assert "python: {environment: shared}" in str(failure.value)


SOURCE = """def sign(x):
    if x > 0:
        return "positive"
    print("negative")
    return "negative"


def never(y):
    return y * 2
"""

COVERAGE = {
    "files": {
        "app/core.py": {
            "executed_lines": [1, 2, 3, 8],
            "missing_lines": [4, 5, 9],
            "missing_branches": [[2, 4]],
            "summary": {
                "covered_lines": 4,
                "num_statements": 7,
                "covered_branches": 1,
                "num_branches": 2,
            },
        },
        "tests/test_core.py": {
            "executed_lines": [1],
            "missing_lines": [],
            "summary": {"covered_lines": 1, "num_statements": 1},
        },
    }
}


def test_coverage_names_the_lines_and_branches_that_never_ran(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "core.py").write_text(SOURCE)
    measured = pytest_tool.coverage_of(COVERAGE, str(tmp_path))
    assert [f.path for f in measured.files] == ["app/core.py"]
    assert (measured.unit, measured.covered, measured.total) == ("lines", 4, 7)
    assert (measured.branches_covered, measured.branches_total) == (1, 2)
    gaps = [(g.line, g.end_line, g.kind, g.detail) for g in measured.files[0].gaps]
    assert gaps == [
        (2, 2, "branch", "2 -> 4: if x > 0:"),
        (4, 5, "line", 'print("negative")'),
        (9, 9, "line", "return y * 2"),
    ]


def test_a_branch_out_of_the_function_names_the_exit(tmp_path):
    report = {"files": {"a.py": {"executed_lines": [1], "missing_branches": [[1, -1]]}}}
    gap = pytest_tool.coverage_of(report, str(tmp_path)).files[0].gaps[0]
    assert (gap.kind, gap.detail) == ("branch", "1 -> exit: ")


def test_a_coverage_run_measures_branches_and_writes_the_project_report(
    tmp_path, monkeypatch
):
    calls = []

    def fake_run(command, *, cwd, timeout_s=300, stdin=None, env=None, read_write=()):
        calls.append((command, env))
        if "json" in command:
            Path(command[-1]).write_text(__import__("json").dumps(COVERAGE))
        else:
            Path(command[-1].split("=", 1)[1]).write_text(REPORT, encoding="utf-8")
        return SimpleNamespace(out="", err="", duration_ms=1, exit_code=0)

    monkeypatch.setattr(pytest_tool.process, "run", fake_run)
    result = pytest_tool.run(root=str(tmp_path), coverage=True)
    (tests, env), (written, _) = calls
    assert tests[tests.index("-m") : tests.index("-m") + 6] == [
        "-m",
        "coverage",
        "run",
        "--branch",
        "-m",
        "pytest",
    ]
    assert env["COVERAGE_FILE"].endswith(".coverage")
    assert written[-1] == str(tmp_path / "coverage.json")
    assert result.total == 3
    assert result.coverage.report == str(tmp_path / "coverage.json")
    assert result.coverage.files[0].path == "app/core.py"


def test_a_project_without_coverage_still_runs_its_tests(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, *, cwd, timeout_s=300, stdin=None, env=None, read_write=()):
        calls.append(command)
        if "coverage" in command:
            return SimpleNamespace(
                out="", err="No module named coverage", duration_ms=1, exit_code=1
            )
        Path(command[-1].split("=", 1)[1]).write_text(REPORT, encoding="utf-8")
        return SimpleNamespace(out="", err="", duration_ms=1, exit_code=0)

    monkeypatch.setattr(pytest_tool.process, "run", fake_run)
    result = pytest_tool.run(root=str(tmp_path), coverage=True)
    assert len(calls) == 2 and "coverage" not in calls[1]
    assert (result.total, result.coverage) == (3, None)
    assert "No coverage: coverage.py is not installed" in result.output


def test_a_real_coverage_run_finds_the_branch_that_never_ran(tmp_path):
    from vis_lang_python.repl import detect_command

    python = detect_command(str(tmp_path))
    check = __import__("subprocess").run([*python, "-c", "import coverage, pytest"])
    if check.returncode:
        pytest.skip("the project interpreter has no coverage.py or pytest")
    (tmp_path / "app.py").write_text(SOURCE)
    (tmp_path / "test_app.py").write_text(
        "from app import sign\n\n\ndef test_sign():\n    assert sign(1) == 'positive'\n"
    )
    result = pytest_tool.run(root=str(tmp_path), coverage=True, timeout_s=120)
    assert result.is_passed is True
    (measured,) = result.coverage.files
    assert measured.path == "app.py"
    assert (measured.branches_covered, measured.branches_total) == (1, 2)
    assert [(g.line, g.kind) for g in measured.gaps] == [
        (2, "branch"),
        (4, "line"),
        (9, "line"),
    ]
