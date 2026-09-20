"""Ruff's report becomes contract diagnostics without losing its rule codes."""

import json

import pytest
from vis_lang_python import ruff_tool


def _report(code, message="undefined name", row=3, column=5):
    return json.dumps(
        [
            {
                "code": code,
                "message": message,
                "filename": "/project/app/main.py",
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
