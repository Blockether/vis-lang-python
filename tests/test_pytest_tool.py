"""Counts and failures are read from the report pytest writes, not its output."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from vis_lang_python import caches, pytest_tool

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


def test_a_run_asks_for_the_caches_and_the_directory_it_reports_into(
    tmp_path, monkeypatch
):
    seen = {}

    def fake_run(command, *, cwd, timeout_s=300, stdin=None, env=None, read_write=()):
        seen["read_write"] = [str(path) for path in read_write]
        seen["report"] = command[-1].split("=", 1)[1]
        Path(seen["report"]).write_text(REPORT, encoding="utf-8")
        return SimpleNamespace(out="", err="", duration_ms=1, exit_code=0)

    monkeypatch.setattr(pytest_tool.process, "run", fake_run)
    pytest_tool.run(root=str(tmp_path))
    granted = seen["read_write"]
    assert caches.uv_cache() in granted
    assert caches.uv_interpreters() in granted
    # The JUnit report is written outside the project, so the run is handed that
    # directory too: confined without it, pytest has nowhere to write the report.
    assert str(Path(seen["report"]).parent) in granted
