"""The managed REPL keeps its globals and never leaves a child behind."""

import os
import shlex
import shutil
import time

import pytest
from vis_lang_interface.process import shell_call

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


def _launcher(monkeypatch, script):
    """Start the REPL through `sh -c script`, a stand-in for `uv run python`."""
    monkeypatch.setattr(
        repl, "detect_command", lambda cwd, **_: ["sh", "-c", script, "launcher"]
    )
    monkeypatch.setattr(repl, "PING_TIMEOUT_S", 1.0)


def test_a_start_waits_while_the_launcher_reports_progress(project, monkeypatch):
    # Blockether/vis#343: `uv run` installed packages for longer than the startup
    # ping budget, and the start killed it in the middle of the installation.
    tools, cwd = project
    _launcher(
        monkeypatch,
        'for n in 1 2 3 4 5 6 7 8 9 10; do echo "installing $n" >&2; sleep 0.3; done;'
        ' exec python3 "$@"',
    )
    started = repl.start({"cwd": cwd})
    assert started["result"] == "started", started
    assert tools.repl_eval("6 * 7", cwd=cwd).value == "42"


def test_a_quiet_launcher_fails_its_start(project, monkeypatch):
    _, cwd = project
    _launcher(monkeypatch, "sleep 30")
    began = time.monotonic()
    failed = repl.start({"cwd": cwd})
    assert failed["result"] == "failed"
    assert "no output and no answer for 1s" in failed["message"]
    assert time.monotonic() - began < 10
    assert repl.status({"cwd": cwd})["status"] == "down"


def test_a_launcher_that_exits_fails_at_once_with_its_error(project, monkeypatch):
    _, cwd = project
    _launcher(monkeypatch, "echo 'a hash was expected' >&2; exit 3")
    failed = repl.start({"cwd": cwd})
    assert failed["result"] == "failed"
    assert failed["exit"] == 3
    assert "a hash was expected" in failed["log_tail"]


# Regression, Blockether/vis#389: a shared-env REPL exited before its ping with an
# empty log, and the error dropped the exit status.
def test_a_silent_exit_names_its_status_and_checks_the_interpreter(
    project, monkeypatch
):
    tools, cwd = project
    _launcher(monkeypatch, "exit 126")
    failed = repl.start({"cwd": cwd})
    assert failed["result"] == "failed"
    assert failed["exit"] == 126
    assert "status 126: the command was found but could not run" in failed["message"]
    assert "log_tail" not in failed
    assert failed["probe"] == {"exit": 126, "output": []}
    with pytest.raises(RuntimeError) as raised:
        tools.repl_start(cwd=cwd)
    text = str(raised.value)
    assert "Command: sh -c exit 126 launcher <vis python driver>" in text
    assert "The interpreter wrote nothing to its log." in text
    assert "A check run of the same interpreter exited with status 126" in text


def test_a_silent_kept_only_failure_says_the_interpreter_works(project, monkeypatch):
    tools, cwd = project
    # Only the kept REPL has a rendezvous, so only it exits.
    _launcher(monkeypatch, '[ -n "$VIS_LANG_RENDEZVOUS" ] && exit 3; exec python3 "$@"')
    with pytest.raises(RuntimeError) as raised:
        tools.repl_start(cwd=cwd)
    text = str(raised.value)
    assert "The interpreter exited with status 3" in text
    assert "only the kept REPL failed" in text
    assert "Check run output:" in text


def test_exit_statuses_name_their_cause():
    assert "could not run" in repl.exit_meaning(126)
    assert repl.exit_meaning(127) == "the command was not found"
    assert repl.exit_meaning(137) == "it was killed by signal 9"
    assert repl.exit_meaning(-15) == "it was killed by signal 15"
    assert repl.exit_meaning(1) == ""
    assert repl.exit_meaning(None) == ""


# Regression, Blockether/vis#389: a kept REPL whose rendezvous was gone made the
# attach start a second child without its driver, which failed with
# FileNotFoundError for driver.py.
def test_a_kept_repl_without_its_rendezvous_is_replaced(project, monkeypatch):
    tools, cwd = project
    first = repl.start({"cwd": cwd})
    repl.detach_all()
    kept = shell_call()({"op": "logs", "id": repl.shell_id(cwd), "offset": -1})
    held = next(w for w in shlex.split(kept["command"]) if w.startswith("3<>"))
    shutil.rmtree(os.path.dirname(held[3:]))
    meetings = []
    real_start = repl.runtime.start

    def counted_start(command, **options):
        meetings.append(options.get("meeting"))
        return real_start(command, **options)

    monkeypatch.setattr(repl.runtime, "start", counted_start)
    again = repl.start({"cwd": cwd})
    assert again["result"] == "started", again
    assert again["pid"] != first["pid"]
    assert None not in meetings
    assert tools.repl_eval("6 * 7", cwd=cwd).value == "42"


# Regression, Blockether/vis#379: `python.environment: shared` must select the
# shared interpreter, and say how to fix a missing `vis-agent`.
def test_a_shared_environment_selects_vis_agent(tmp_path, monkeypatch):
    (tmp_path / "uv.lock").write_text("")
    monkeypatch.setattr(repl.shutil, "which", lambda name: f"/bin/{name}")
    assert repl.detect_command(str(tmp_path)) == ["uv", "run", "python"]
    assert repl.detect_command(str(tmp_path), shared=True) == [
        "/bin/vis-agent",
        "python",
        "--shared",
    ]
    # A kept REPL of either environment is found again after a restart.
    assert [command[0] for command in repl._kept_commands(str(tmp_path))] == [
        "uv",
        "/bin/vis-agent",
    ]


def test_a_shared_environment_without_vis_agent_names_the_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(repl.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="vis-agent is not on PATH"):
        repl.detect_command(str(tmp_path), shared=True)


def test_a_broken_virtualenv_start_names_both_fixes():
    tail = "error: Project virtual environment directory `.venv` cannot be used because it is not a valid Python environment"
    assert "uv sync" in repl.environment_hint(tail)
    assert repl.environment_hint("ModuleNotFoundError: x") == ""
