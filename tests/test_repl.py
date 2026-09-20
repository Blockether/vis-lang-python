"""The managed REPL keeps its globals and never leaves a child behind."""

import pytest

from vis_lang_python import caches, repl
from vis_lang_python.tools import PythonTools


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    tools = PythonTools()
    yield tools, str(tmp_path)
    tools.repl_stop(cwd=str(tmp_path))


def test_status_is_down_before_a_start(project):
    tools, cwd = project
    assert tools.repl_status(cwd=cwd).is_running is False


def test_globals_survive_between_evaluations(project):
    tools, cwd = project
    started = tools.repl_start(cwd=cwd)
    assert started.is_running is True
    assert started.command
    tools.repl_eval("kept = 41", cwd=cwd)
    answer = tools.repl_eval("kept + 1", cwd=cwd)
    assert answer.value == "42"
    assert answer.error == ""


def test_output_and_errors_come_back(project):
    tools, cwd = project
    tools.repl_start(cwd=cwd)
    printed = tools.repl_eval("print('hello')", cwd=cwd)
    assert printed.output.strip() == "hello"
    failed = tools.repl_eval("1 / 0", cwd=cwd)
    assert "ZeroDivisionError" in failed.error


def test_a_second_start_keeps_the_live_repl(project):
    tools, cwd = project
    first = tools.repl_start(cwd=cwd)
    tools.repl_eval("kept = 7", cwd=cwd)
    again = tools.repl_start(cwd=cwd)
    assert again.detail == "already-running"
    assert again.id == first.id
    assert tools.repl_eval("kept", cwd=cwd).value == "7"


def test_stopping_without_a_repl_is_safe(project):
    tools, cwd = project
    stopped = tools.repl_stop(cwd=cwd)
    assert stopped.is_running is False
    assert stopped.detail == "not-managed"


def test_evaluating_without_a_repl_says_so(project):
    tools, cwd = project
    with pytest.raises(RuntimeError, match="not up"):
        tools.repl_eval("1 + 1", cwd=cwd)


def test_the_interpreter_is_handed_the_caches_it_downloads_into(tmp_path, monkeypatch):
    seen = {}

    def fake_start(
        command, *, cwd=None, env=None, read_write=(), name="runtime", meeting=None
    ):
        seen["read_write"] = [str(path) for path in read_write]
        raise RuntimeError("not starting a real interpreter here")

    monkeypatch.setattr(repl.runtime, "start", fake_start)
    with pytest.raises(RuntimeError, match="not starting"):
        repl.start({"cwd": str(tmp_path)})
    assert caches.uv_cache() in seen["read_write"]
    assert caches.uv_interpreters() in seen["read_write"]
