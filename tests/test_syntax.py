"""Private edit hooks use the project's compiler without executing source."""

import json
import runpy
import sys
from pathlib import Path

import blockether.vis.extension as vis
import pytest
from vis_lang_interface import ToolRun, ToolTimeout

from vis_lang_python import tools


@pytest.fixture
def compiler(monkeypatch):
    monkeypatch.setattr(tools.repl, "detect_command", lambda root: [sys.executable])
    return tools._check_syntax


@pytest.mark.parametrize(
    "source",
    [
        "",
        "value = 'Zażółć'\n",
        "from unavailable_package import missing\n",
        "raise RuntimeError('must not execute')\n",
        "def example(value: Unknown) -> Other: ...\n",
    ],
)
def test_valid_sources_compile_without_evaluation(compiler, tmp_path, source):
    result = compiler({"example.py": source}, tmp_path)
    assert (result.language, result.files, result.is_clean) == ("python", 1, True)
    assert result.diagnostics == ()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "source",
    ["def broken(\n", "if True\n    pass\n", "return 1\n", "continue\n"],
)
def test_the_compiler_reports_syntax_and_scope_errors(compiler, tmp_path, source):
    result = compiler({"example.py": source}, tmp_path)
    assert (result.files, result.is_clean) == (1, False)
    (diagnostic,) = result.diagnostics
    assert diagnostic.path == "example.py"
    assert diagnostic.line == 1 and diagnostic.column > 0
    assert diagnostic.level == "error" and diagnostic.message


def test_preview_text_is_checked_without_reading_or_writing_source(compiler, tmp_path):
    path = tmp_path / "example.py"
    path.write_text("value = 1\n")
    result = compiler({str(path): "value = (\n"}, tmp_path)
    assert not result.is_clean
    assert path.read_text() == "value = 1\n"
    assert not (tmp_path / "__pycache__").exists()


def test_empty_sources_do_not_start_an_interpreter(monkeypatch, tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail("an empty check must not start a process")

    monkeypatch.setattr(tools.process, "run", unexpected)
    result = tools._check_syntax({}, tmp_path)
    assert result.is_clean and result.files == 0


def test_each_project_uses_its_own_interpreter(monkeypatch, tmp_path):
    roots = [tmp_path / name for name in ("first", "second")]
    for root in roots:
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'example'\n")
    monkeypatch.setattr(tools.repl, "detect_command", lambda root: [f"{root}/python"])
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return ToolRun(tuple(command), 0, "[]", "", 0)

    monkeypatch.setattr(tools.process, "run", run)
    sources = {"first/a.py": "pass", "second/b.pyi": "value: int"}
    result = tools._check_syntax(sources, tmp_path)
    assert result.is_clean and result.files == 2
    for (command, options), root, (path, text) in zip(calls, roots, sources.items()):
        assert command == [f"{root}/python", "-c", tools._SYNTAX_DRIVER]
        assert options["cwd"] == str(root)
        assert json.loads(options["stdin"]) == {path: text}
        assert options["timeout_s"] == tools._CHECK_TIMEOUT_S
        assert options["read_write"] == tools.caches.granted_paths()
    assert len(calls) == 2


@pytest.fixture
def extension(compiler, monkeypatch, tmp_path):
    registered = []
    monkeypatch.setattr(vis, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(vis, "state", {})
    monkeypatch.setattr(vis, "register_extension", registered.append)
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "extension.py"))
    return registered[0]


def test_registered_hook_refuses_only_a_breaking_python_patch(extension, tmp_path):
    path = tmp_path / "example.py"
    path.write_text("value = 1\n")
    hook = extension.op_hooks[0]
    preview = {"path": str(path), "before": "value = 1\n", "after": "value = (\n"}
    refusal = hook.fn({"op": "patch", "preview": preview})
    assert refusal["marker"] == "block"
    assert "nothing was written" in refusal["reason"]
    assert path.read_text() == preview["before"]
    assert hook.fn({"preview": {**preview, "before": "value = [\n"}}) is None
    assert hook.fn({"preview": {**preview, "after": "value = 2\n"}}) is None
    assert hook.fn({"preview": {**preview, "path": "example.clj"}}) is None


def test_registered_hooks_report_python_writes_until_repaired(extension, tmp_path):
    path = tmp_path / "example.pyi"
    path.write_text("value: int\n")
    before, after = extension.op_hooks[1:]
    call = {"op": "python_execution", "args": [], "result": {}}
    before.fn(call)
    path.write_text("value: [\n")
    after.fn(call)
    report = extension.ctx()["python_syntax_errors"]
    assert len(report) == 1 and report[0].startswith("example.pyi:1:")
    before.fn(call)
    path.write_text("value: int\n")
    after.fn(call)
    assert extension.ctx() == {}


@pytest.mark.parametrize("failure", ["exit", "timeout", "invalid-json"])
def test_toolchain_failures_reach_the_guard_without_a_false_syntax_verdict(
    extension, monkeypatch, tmp_path, failure
):
    def run(command, **options):
        if failure == "timeout":
            raise ToolTimeout("interpreter did not answer")
        return ToolRun(tuple(command), 1 if failure == "exit" else 0, "bad", "", 0)

    monkeypatch.setattr(tools.process, "run", run)
    path = tmp_path / "example.py"
    path.write_text("pass\n")
    with pytest.raises((RuntimeError, ValueError)):
        tools._check_syntax({str(path): "value = (\n"}, tmp_path)
    assert (
        extension.op_hooks[0].fn(
            {"preview": {"path": str(path), "before": "pass\n", "after": "value = (\n"}}
        )
        is None
    )
    assert extension.ctx() == {}
