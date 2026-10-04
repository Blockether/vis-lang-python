"""Parity with the MIT-licensed clj-parinferish Python repair corpus.

The fixture keeps the original scrubbed inputs and expected output, without a JVM.
"""

import ast
import json
from pathlib import Path

import pytest

from vis_lang_python.repair import repair, repair_source

CASES = json.loads((Path(__file__).parent / "fixtures/repair_corpus.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_legacy_corpus(case):
    result = repair(case["source"])
    assert result.source == case["expected"]
    assert list(result.notes) == case["fixes"]
    assert [problem.message for problem in result.problems] == case["problems"]


def parses_clean(source):
    try:
        compile(source, "<repair>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    except (SyntaxError, ValueError):
        return False
    return True


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_repair_proposal_is_valid_or_absent(case):
    source = case["source"]
    result = repair_source(source, parses_clean=parses_clean)
    if parses_clean(source):
        assert result is None
    elif result is not None:
        assert parses_clean(result.source)
        assert result.notes
    if case["result"] == "repaired" and parses_clean(case["expected"]):
        assert result is not None


def test_error_line_constrains_balanced_statement_repair():
    source = "x = foo(1, 2\ny = bar(3))\n"
    expected = "x = foo(1, 2)\ny = bar(3)\n"
    assert repair(source).source == expected
    assert repair(source, error_line=1).source == expected
    assert repair(source, error_line=2).source == expected
    assert repair(source, error_line=5).source == source


@pytest.mark.parametrize("source", ["return 1", "continue", "if True\n    pass\n"])
def test_proposal_refuses_nonstructural_errors(source):
    assert repair_source(source, parses_clean=parses_clean) is None


def test_proposal_never_executes_code(tmp_path):
    target = tmp_path / "side_effect"
    source = f"open({str(target)!r}, 'w').write('not executed')\nprint(42"
    result = repair_source(source, parses_clean=parses_clean)
    assert result is not None
    assert not target.exists()


def test_file_proposal_does_not_change_an_untouched_line():
    original = "first = call(1,\nsecond = call(2))\n"
    source = "first = call(1\nsecond = call(2))\n"
    assert parses_clean(original)
    assert (
        repair_source(
            source, original=original, spans=[(1, 1)], parses_clean=parses_clean
        )
        is None
    )


def test_file_proposal_repairs_only_the_changed_line():
    original = "first = 1\nsecond = [2]\n"
    source = "first = 1\nsecond = [2\n"
    result = repair_source(
        source, original=original, spans=[(2, 2)], parses_clean=parses_clean
    )
    assert result is not None and result.source == original


def test_valid_async_block_needs_no_repair():
    assert repair_source("await function()\n", parses_clean=parses_clean) is None


def test_async_block_is_repaired_without_running_it():
    result = repair_source("await function(\n", parses_clean=parses_clean)
    assert result is not None and result.source == "await function()\n"


@pytest.mark.parametrize(
    "unit", ["(", ")", "[{(", 'x = "abc\n', "f(a; b)\n", "a \\ b\n"]
)
def test_large_inputs_keep_the_scan_budget_bounded(monkeypatch, unit):
    from vis_lang_python.repair import Work

    scanned = 0
    original_rescan = Work.rescan

    def counted(work, candidate, scanner):
        nonlocal scanned
        scanned += 1
        return original_rescan(work, candidate, scanner)

    monkeypatch.setattr(Work, "rescan", counted)
    source = unit * 2000
    result = repair(source)
    assert len(result.fixes) <= 32
    # A repair scans at most a small multiple of its input, not all combinations.
    assert scanned <= 130
