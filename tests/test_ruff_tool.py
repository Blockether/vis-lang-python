"""Ruff's report becomes contract diagnostics without losing its rule codes."""

import json

import pytest

from vis_lang_python import ruff_tool


def _report(
    code, message="undefined name", row=3, column=5, filename="/project/app/main.py"
):
    return json.dumps(
        [
            {
                "code": code,
                "message": message,
                "filename": filename,
                "location": {"row": row, "column": column},
            }
        ]
    )


def test_a_style_rule_is_a_warning():
    found = ruff_tool.diagnostics_of(_report("E501", "line too long"), "/project")
    assert found[0].level == "warning"
    assert found[0].rule == "E501"
    assert found[0].path == "app/main.py"
    assert (found[0].line, found[0].column) == (3, 5)


def test_a_broken_code_rule_is_an_error():
    assert ruff_tool.diagnostics_of(_report("F821"))[0].level == "error"
    assert ruff_tool.diagnostics_of(_report("E902", "unreadable"))[0].level == "error"
    assert ruff_tool.diagnostics_of(_report("E999", "syntax"))[0].level == "error"
    # Regression: ruff names a syntax error `invalid-syntax` rather than a rule
    # code, and such findings were reported as style warnings.
    assert (
        ruff_tool.diagnostics_of(_report("invalid-syntax", "syntax"))[0].level
        == "error"
    )
    assert (
        ruff_tool.diagnostics_of(_report("E501", "line too long"))[0].level == "warning"
    )


def test_a_finding_without_a_rule_is_an_error():
    assert ruff_tool.diagnostics_of(_report(None))[0].level == "error"


def test_an_empty_report_has_no_findings():
    assert ruff_tool.diagnostics_of("") == ()
    assert ruff_tool.diagnostics_of("[]") == ()


def test_a_non_json_report_is_refused():
    with pytest.raises(ValueError, match="did not answer with JSON"):
        ruff_tool.diagnostics_of("error: ruff exploded")


@pytest.mark.skipif(not ruff_tool.shutil.which("ruff"), reason="ruff is not installed")
def test_formatting_a_source_string_round_trips(tmp_path):
    result = ruff_tool.format_source("x = [1,2]\n", str(tmp_path))
    assert result.source == "x = [1, 2]\n"


@pytest.mark.skipif(not ruff_tool.shutil.which("ruff"), reason="ruff is not installed")
def test_formatting_a_file_rewrites_it_only_when_asked(tmp_path):
    messy = tmp_path / "messy.py"
    messy.write_text("x = [1,2]\n")
    found = ruff_tool.format_files([str(messy)], str(tmp_path))
    assert found.changed == (str(messy),)
    assert messy.read_text() == "x = [1,2]\n"
    written = ruff_tool.format_files([str(messy)], str(tmp_path), is_written=True)
    assert written.is_written
    assert messy.read_text() == "x = [1, 2]\n"


def _stub_ruff(monkeypatch, run):
    """Run ruff's callers against `run` instead of an installed ruff."""
    monkeypatch.setattr(ruff_tool, "ruff_path", lambda root: "ruff")
    monkeypatch.setattr(ruff_tool.process, "run", run)


def test_the_files_that_differ_come_from_ruffs_output(tmp_path, monkeypatch):
    """Ruff's report wording changes between versions; its output does not."""
    messy = tmp_path / "messy.py"
    messy.write_text("x = [1,2]\n")
    tidy = tmp_path / "tidy.py"
    tidy.write_text("y = 1\n")
    formatted = {str(messy): "x = [1, 2]\n", str(tidy): "y = 1\n"}

    def run(command, *, cwd=None, timeout_s=300, stdin=None, env=None):
        if "--check" in command:
            return ruff_tool.process.ToolRun(tuple(command), 1, "unformatted", "", 0)
        name = command[command.index("--stdin-filename") + 1]
        return ruff_tool.process.ToolRun(tuple(command), 0, formatted[name], "", 0)

    _stub_ruff(monkeypatch, run)
    names = [str(messy), str(tidy)]
    found = ruff_tool.format_files(names, str(tmp_path))
    assert (found.changed, found.unchanged) == ((str(messy),), (str(tidy),))
    assert not found.is_written
    assert messy.read_text() == "x = [1,2]\n"
    written = ruff_tool.format_files(names, str(tmp_path), is_written=True)
    assert written.is_written
    assert messy.read_text() == "x = [1, 2]\n"


def test_a_formatted_tree_runs_ruff_once(tmp_path, monkeypatch):
    """Nothing differs, so no file is read and no file is written."""
    commands = []

    def run(command, *, cwd=None, timeout_s=300, stdin=None, env=None):
        commands.append(command)
        return ruff_tool.process.ToolRun(tuple(command), 0, "", "", 0)

    _stub_ruff(monkeypatch, run)
    found = ruff_tool.format_files(["a.py", "b.py"], str(tmp_path), is_written=True)
    assert (found.changed, found.unchanged) == ((), ("a.py", "b.py"))
    assert not found.is_written
    assert len(commands) == 1


def test_source_text_is_linted_from_stdin(tmp_path, monkeypatch):
    # Regression: only files could be linted. Ruff names text it reads on stdin
    # `-`; findings for it are reported as `<stdin>`, as the Clojure tools do.
    seen = {}

    def run(command, *, cwd=None, timeout_s=300, stdin=None, env=None):
        seen.update(command=command, cwd=cwd, stdin=stdin)
        report = _report("F821", filename="-")
        return ruff_tool.process.ToolRun(tuple(command), 1, report, "", 0)

    _stub_ruff(monkeypatch, run)
    found = ruff_tool.check_source("x = y\n", str(tmp_path))
    assert seen["command"][1:3] == ["check", "--output-format"]
    assert seen["command"][-1] == "-"
    assert (seen["cwd"], seen["stdin"]) == (str(tmp_path), "x = y\n")
    assert found.files == 1
    assert [(one.path, one.level, one.rule) for one in found.diagnostics] == [
        ("<stdin>", "error", "F821")
    ]


def test_a_source_ruff_cannot_check_is_refused(tmp_path, monkeypatch):
    def run(command, *, cwd=None, timeout_s=300, stdin=None, env=None):
        return ruff_tool.process.ToolRun(tuple(command), 2, "", "bad config", 0)

    _stub_ruff(monkeypatch, run)
    with pytest.raises(RuntimeError, match="bad config"):
        ruff_tool.check_source("x = 1\n", str(tmp_path))


@pytest.mark.skipif(not ruff_tool.shutil.which("ruff"), reason="ruff is not installed")
def test_linting_a_source_string_reports_ruffs_findings(tmp_path):
    found = ruff_tool.check_source("import os\nx = undefined_name\n", str(tmp_path))
    assert {one.rule for one in found.diagnostics} >= {"F401", "F821"}
    assert {one.path for one in found.diagnostics} == {"<stdin>"}
