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
    status = tools.repl_status(cwd=cwd)
    assert status.is_running is False
    assert status.detail == "not running"


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


def test_a_missing_closer_is_put_back_before_the_code_runs(project):
    # An agent that loses count of closing brackets must not fight the parser.
    tools, cwd = project
    tools.repl_start(cwd=cwd)
    answer = tools.repl_eval("values = [1, 2, 3\nsum(values)", cwd=cwd)
    assert (answer.value, answer.error) == ("6", "")
    assert answer.repairs == (
        "line 1: added ']' at column 18 to close '[' from line 1",
    )
    assert answer.code == "values = [1, 2, 3]\nsum(values)"


def test_code_that_does_not_parse_runs_none_of_its_statements(project):
    tools, cwd = project
    tools.repl_start(cwd=cwd)
    refused = tools.repl_eval("ran = True\nx = = 2", cwd=cwd)
    first, second, *rest = refused.error.splitlines()
    assert first.startswith("Line 2, column ")
    assert first.endswith(". The code was not evaluated.")
    assert second == "No safe repair exists. Fix the syntax, then evaluate again."
    assert "SyntaxError" in rest[-1]
    assert refused.repairs == ()
    assert "NameError" in tools.repl_eval("ran", cwd=cwd).error


def test_a_compile_error_after_a_statement_runs_nothing(project):
    # The parser accepts a top-level `yield`; only the compiler refuses it.
    tools, cwd = project
    tools.repl_start(cwd=cwd)
    refused = tools.repl_eval("ran = True\n(yield 1)", cwd=cwd)
    assert "'yield' outside function. The code was not evaluated." in refused.error
    assert "NameError" in tools.repl_eval("ran", cwd=cwd).error


def test_a_second_start_keeps_the_live_repl(project):
    tools, cwd = project
    first = tools.repl_start(cwd=cwd)
    tools.repl_eval("kept = 7", cwd=cwd)
    again = tools.repl_start(cwd=cwd)
    assert again.detail == "already running"
    assert again.id == first.id
    assert tools.repl_status(cwd=cwd).detail == "running"
    assert tools.repl_eval("kept", cwd=cwd).value == "7"


def test_a_repl_outlives_a_sandbox_restart(project):
    # A sandbox restart ended the Python process, and with it the interpreter
    # and its globals. Now the next process attaches to the same interpreter.
    tools, cwd = project
    first = repl.start({"cwd": cwd, "env_fingerprint": {"TOKEN": "sha256:ab"}})
    assert first["result"] == "started"
    tools.repl_eval("kept = 41", cwd=cwd)
    repl.detach_all()
    status = repl.status({"cwd": cwd})
    assert (status["status"], status["pid"]) == ("up", first["pid"])
    assert status["env"] == {"TOKEN": "sha256:ab"}
    assert tools.repl_eval("kept + 1", cwd=cwd).value == "42"
    repl.detach_all()
    again = repl.start({"cwd": cwd})
    assert (again["result"], again["pid"]) == ("already-running", first["pid"])
    repl.detach_all()
    assert tools.repl_stop(cwd=cwd).detail == "stopped"
    assert tools.repl_status(cwd=cwd).detail == "not running"


def test_stopping_without_a_repl_is_safe(project):
    tools, cwd = project
    stopped = tools.repl_stop(cwd=cwd)
    assert stopped.is_running is False
    assert stopped.detail == "not managed"


def test_evaluating_without_a_repl_says_so(project):
    tools, cwd = project
    with pytest.raises(RuntimeError, match="not up"):
        tools.repl_eval("1 + 1", cwd=cwd)


def test_the_interpreter_is_handed_the_caches_it_downloads_into(tmp_path, monkeypatch):
    seen = {}

    def fake_start(
        command,
        *,
        cwd=None,
        env=None,
        read_write=(),
        name="runtime",
        meeting=None,
        shell_id=None,
    ):
        seen["read_write"] = [str(path) for path in read_write]
        raise RuntimeError("not starting a real interpreter here")

    monkeypatch.setattr(repl.runtime, "start", fake_start)
    with pytest.raises(RuntimeError, match="not starting"):
        repl.start({"cwd": str(tmp_path)})
    assert caches.uv_cache() in seen["read_write"]
    assert caches.uv_interpreters() in seen["read_write"]


def test_a_long_value_comes_back_pretty_printed(project):
    tools, cwd = project
    tools.repl_start(cwd=cwd)
    answer = tools.repl_eval("{f'key{n}': list(range(n)) for n in range(6)}", cwd=cwd)
    assert answer.value.splitlines() == [
        "{'key0': [],",
        " 'key1': [0],",
        " 'key2': [0, 1],",
        " 'key3': [0, 1, 2],",
        " 'key4': [0, 1, 2, 3],",
        " 'key5': [0, 1, 2, 3, 4]}",
    ]
    assert tools.repl_eval("[1, 2, 3]", cwd=cwd).value == "[1, 2, 3]"
    assert tools.repl_eval("'x' * 100", cwd=cwd).value == repr("x" * 100)
