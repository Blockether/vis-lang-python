"""A REPL start installs what the project declares (Blockether/vis#478)."""

import ast
import os
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
from vis_lang_interface import process

from vis_lang_python import environment, repl
from vis_lang_python.tools import PythonTools

pytestmark = pytest.mark.installs

PROJECT = "[project]\nname = 'x'\nversion = '0'\n"


class Installer:
    """A uv that records its commands and leaves what the real one leaves."""

    def __init__(self, root: Path):
        self.root = root
        self.commands: list[tuple[str, ...]] = []
        self.exit_code = 0
        self.err = ""
        self.raises = None

    def said(self):
        return [" ".join(command[1:]) for command in self.commands]

    def environment(self, name=".venv"):
        python = self.root / name / "bin" / "python"
        python.parent.mkdir(parents=True, exist_ok=True)
        if not python.exists():
            python.symlink_to(sys.executable)
        (self.root / name / "pyvenv.cfg").write_text("home = /usr\n")
        return python

    def install(self, python: Path, name="dep-1.0"):
        packages = python.parent.parent / "lib" / "python3.13" / "site-packages"
        (packages / f"{name}.dist-info").mkdir(parents=True, exist_ok=True)

    def __call__(self, command, root, env, timeout_s):
        self.commands.append(tuple(command))
        if self.raises:
            raise self.raises
        out = ""
        if self.exit_code == 0 and "--shared" not in command:
            if "venv" in command:
                self.environment()
            elif "run" in command:
                python = self.environment()
                self.install(python)
                out = f"{python}\n"
            else:
                self.install(Path(command[command.index("--python") + 1]))
        return process.ToolRun(tuple(command), self.exit_code, out, self.err, 1)


@pytest.fixture
def uv(tmp_path, monkeypatch):
    fake = Installer(tmp_path)
    monkeypatch.setattr(environment, "installer", lambda shared=False: (("uv",), "uv"))
    monkeypatch.setattr(environment, "_run", fake)
    return fake


def test_pyproject_wins_over_requirements(tmp_path, uv):
    (tmp_path / "pyproject.toml").write_text(PROJECT)
    (tmp_path / "requirements.txt").write_text("six\n")
    ready = environment.prepare(str(tmp_path))
    assert uv.said() == [f"run python -c {environment.PRINT_EXECUTABLE}"]
    assert ready.sync.state == "installed"
    assert ready.python == (str(tmp_path / ".venv" / "bin" / "python"),)


@pytest.mark.parametrize(
    "table", ["[project]\nname = 'x'", "[dependency-groups]\ndev = []"]
)
def test_each_table_that_declares_dependencies_wins(tmp_path, uv, table):
    (tmp_path / "pyproject.toml").write_text(f"{table}\n")
    (tmp_path / "requirements.txt").write_text("six\n")
    environment.prepare(str(tmp_path))
    assert uv.said() == [f"run python -c {environment.PRINT_EXECUTABLE}"]


@pytest.mark.parametrize(
    "tables", ["[tool.ruff]\nline-length = 100\n", "[tool.uv]\nlink-mode = 'copy'\n"]
)
def test_a_pyproject_with_only_tool_configuration_installs_the_requirements(
    tmp_path, uv, tables
):
    # `uv run` there would write a uv.lock and install nothing of the project.
    (tmp_path / "pyproject.toml").write_text(tables)
    (tmp_path / "requirements.txt").write_text("six\n")
    python = str(tmp_path / ".venv" / "bin" / "python")
    assert environment.prepare(str(tmp_path)).python == (python,)
    assert uv.said() == [
        "venv --clear .venv",
        f"pip install --python {python} -r requirements.txt",
    ]


def test_a_uv_table_alone_is_a_uv_project(tmp_path, uv):
    (tmp_path / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = []\n")
    environment.prepare(str(tmp_path))
    assert uv.said() == [f"run python -c {environment.PRINT_EXECUTABLE}"]


def test_an_existing_environment_is_used_again(tmp_path, uv):
    (tmp_path / "requirements.txt").write_text("six\n")
    python = uv.environment("venv")
    assert environment.prepare(str(tmp_path)).python == (str(python),)
    assert uv.said() == [f"pip install --python {python} -r requirements.txt"]


def test_a_project_without_dependency_files_installs_nothing(tmp_path, uv):
    assert environment.prepare(str(tmp_path)) == environment.Environment(
        (), environment.Sync("none")
    )
    # A Poetry project is not uv's, also on a machine without `poetry`.
    (tmp_path / "pyproject.toml").write_text(PROJECT)
    (tmp_path / "poetry.lock").write_text("")
    for shared in (False, True):
        assert environment.prepare(str(tmp_path), shared=shared).sync.state == "none"
    assert uv.commands == []


@pytest.mark.parametrize(
    ("tables", "sources"),
    [
        (PROJECT, "-r pyproject.toml"),
        (PROJECT + "[dependency-groups]\ndev = []\n", "-r pyproject.toml --group dev"),
        ("[dependency-groups]\ndev = []\n", "--group dev"),
    ],
)
def test_an_environment_made_by_hand_gets_the_dependencies(
    tmp_path, uv, tables, sources
):
    # `uv run` would make a `.venv` next to it and write a lock into the project.
    (tmp_path / "pyproject.toml").write_text(tables)
    python = str(uv.environment("venv"))
    assert environment.prepare(str(tmp_path)).python == (python,)
    assert uv.said() == [f"pip install --python {python} {sources}"]
    assert not (tmp_path / ".venv").exists()


def test_a_group_that_uv_does_not_install_declares_nothing(tmp_path, uv):
    (tmp_path / "pyproject.toml").write_text("[dependency-groups]\ndocs = []\n")
    uv.environment("venv")
    assert environment.prepare(str(tmp_path)).sync.state == "none"
    assert uv.commands == []


def test_a_uv_project_keeps_its_own_environment(tmp_path, uv):
    (tmp_path / "pyproject.toml").write_text(PROJECT)
    (tmp_path / "uv.lock").write_text("")
    uv.environment("venv")
    environment.prepare(str(tmp_path))
    assert uv.said() == [f"run python -c {environment.PRINT_EXECUTABLE}"]


def test_an_unchanged_project_runs_no_installer(tmp_path, uv):
    (tmp_path / "requirements.txt").write_text("-r base.txt\n")
    (tmp_path / "base.txt").write_text("six\n")
    first = environment.prepare(str(tmp_path))
    done = len(uv.commands)
    again = environment.prepare(str(tmp_path))
    assert (again.sync.state, again.python, len(uv.commands)) == (
        "current",
        first.python,
        done,
    )


def test_a_link_to_the_project_is_the_same_project(tmp_path, uv, monkeypatch):
    # macOS reaches a temporary directory as /var/... and as /private/var/...
    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.setattr(uv, "root", project)
    (project / "requirements.txt").write_text("six\n")
    (tmp_path / "link").symlink_to(project)
    first = environment.prepare(str(project))
    done = len(uv.commands)
    linked = environment.prepare(str(tmp_path / "link"))
    assert (linked.sync.state, linked.python, len(uv.commands)) == (
        "current",
        first.python,
        done,
    )
    assert environment.inputs(str(tmp_path / "link")) == first.inputs


@pytest.mark.parametrize(
    "change",
    [
        lambda root: (root / "base.txt").write_text("six\nidna\n"),
        lambda root: shutil.rmtree(
            next(root.glob(".venv/lib/*/site-packages/dep-1.0.dist-info"))
        ),
        lambda root: (
            next(root.glob(".venv/lib/*/site-packages"))
            .joinpath("extra-2.0.dist-info")
            .mkdir()
        ),
        lambda root: shutil.rmtree(root / ".venv"),
    ],
    ids=["included file", "package removed", "package added", "environment deleted"],
)
def test_a_change_makes_the_next_start_install_again(tmp_path, uv, change):
    (tmp_path / "requirements.txt").write_text("-r base.txt\n")
    (tmp_path / "base.txt").write_text("six\n")
    environment.prepare(str(tmp_path))
    done = len(uv.commands)
    change(tmp_path)
    assert environment.prepare(str(tmp_path)).sync.state == "installed"
    assert len(uv.commands) > done


def test_an_import_that_writes_bytecode_keeps_the_stamp(tmp_path, uv):
    (tmp_path / "requirements.txt").write_text("six\n")
    environment.prepare(str(tmp_path))
    next(tmp_path.glob(".venv/lib/*/site-packages")).joinpath("__pycache__").mkdir()
    assert environment.prepare(str(tmp_path)).sync.state == "current"


def test_a_failed_installation_without_an_environment_fails_the_start(tmp_path, uv):
    (tmp_path / "pyproject.toml").write_text(PROJECT)
    uv.exit_code, uv.err = (
        2,
        "error: Failed to fetch: `http://127.0.0.1:9/simple/six/`\n",
    )
    with pytest.raises(RuntimeError) as raised:
        PythonTools().repl_start(cwd=str(tmp_path))
    text = str(raised.value)
    assert "the project has no environment to run in" in text
    assert "the installer exited with status 2" in text
    assert f"Installer command: uv run python -c {environment.PRINT_EXECUTABLE}" in text
    assert "error: Failed to fetch: `http://127.0.0.1:9/simple/six/`" in text
    assert PythonTools().repl_status(cwd=str(tmp_path)).is_running is False


def test_the_empty_environment_of_a_failed_installer_is_no_environment(
    tmp_path, uv, monkeypatch
):
    # uv makes `.venv` before it resolves. After a failed download that
    # directory holds none of the project's packages.
    (tmp_path / "pyproject.toml").write_text(PROJECT)
    uv.exit_code = 2
    run = uv.__call__

    def makes_the_environment_then_fails(command, root, env, timeout_s):
        uv.environment()
        return run(command, root, env, timeout_s)

    monkeypatch.setattr(environment, "_run", makes_the_environment_then_fails)
    for _ in range(2):
        ready = environment.prepare(str(tmp_path))
        assert (ready.sync.state, ready.python) == ("failed", ())
        assert not (tmp_path / ".venv").exists()


def test_a_failed_installation_removes_only_what_it_made(tmp_path, uv, monkeypatch):
    # uv refuses to clear a `.venv` that is no virtualenv. It stays as it was.
    (tmp_path / "requirements.txt").write_text("six\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "notes.txt").write_text("mine")
    uv.exit_code = 2
    assert environment.prepare(str(tmp_path)).sync.state == "failed"
    assert (tmp_path / ".venv" / "notes.txt").read_text() == "mine"
    # A lock that the failed installer wrote would make a uv project of this one.
    shutil.rmtree(tmp_path / ".venv")
    (tmp_path / "pyproject.toml").write_text(PROJECT)
    run = uv.__call__

    def locks_then_fails(command, root, env, timeout_s):
        (tmp_path / "uv.lock").write_text("")
        return run(command, root, env, timeout_s)

    monkeypatch.setattr(environment, "_run", locks_then_fails)
    assert environment.prepare(str(tmp_path)).sync.state == "failed"
    assert not (tmp_path / "uv.lock").exists()


def test_a_virtualenv_without_interpreter_is_made_again_and_said(tmp_path, uv):
    (tmp_path / "requirements.txt").write_text("six\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /gone\n")
    sync = environment.prepare(str(tmp_path)).sync
    assert uv.said()[0] == "venv --clear .venv"
    assert environment.describe(sync).endswith(
        "; the `.venv` had no interpreter, so uv made it again"
    )


def test_a_program_from_path_is_no_virtualenv(tmp_path, monkeypatch):
    # `python3` is relative: its "environment" would be the working directory.
    (tmp_path / "pyvenv.cfg").write_text("home = /usr\n")
    monkeypatch.chdir(tmp_path)
    assert environment.activation(("python3",)) == {}


def test_a_failed_installation_keeps_an_environment_made_by_hand(tmp_path, uv):
    (tmp_path / "requirements.txt").write_text("nope\n")
    python = str(uv.environment())
    uv.exit_code = 1
    assert environment.prepare(str(tmp_path)).python == (python,)
    assert os.path.isfile(python)


def test_two_starts_at_once_install_once(tmp_path, uv, monkeypatch):
    (tmp_path / "requirements.txt").write_text("six\n")
    run = uv.__call__

    def slow(command, root, env, timeout_s):
        time.sleep(0.2)
        return run(command, root, env, timeout_s)

    monkeypatch.setattr(environment, "_run", slow)
    tools, cwd, details = PythonTools(), str(tmp_path), []
    starts = [
        threading.Thread(
            target=lambda: details.append(tools.repl_start(cwd=cwd).detail)
        )
        for _ in range(2)
    ]
    try:
        for start in starts:
            start.start()
        for start in starts:
            start.join()
        assert len(uv.commands) == 2
        assert sorted(detail.split(" · ")[0] for detail in details) == [
            "already running",
            "started",
        ]
        assert tools.repl_eval("1 + 1", cwd=cwd).value == "2"
    finally:
        tools.repl_stop(cwd=cwd)


def test_the_programs_of_the_environment_are_on_the_path_of_the_repl(tmp_path, uv):
    # `uv run python` put them there. The interpreter now starts without it.
    (tmp_path / "pyproject.toml").write_text(PROJECT)
    tools, cwd = PythonTools(), str(tmp_path)
    try:
        tools.repl_start(cwd=cwd)
        seen = tools.repl_eval(
            "import os; [os.environ['PATH'].split(os.pathsep)[0],"
            " os.environ['VIRTUAL_ENV']]",
            cwd=cwd,
        ).value
        home = str(tmp_path / ".venv")
        assert ast.literal_eval(seen) == [os.path.join(home, "bin"), home]
    finally:
        tools.repl_stop(cwd=cwd)


def test_a_failed_installation_reports_and_starts_on_the_existing_environment(
    tmp_path, uv
):
    (tmp_path / "requirements.txt").write_text("six\n")
    uv.environment()
    uv.exit_code, uv.err = 1, "error: No solution found: nope was not found\n"
    tools = PythonTools()
    try:
        started = tools.repl_start(cwd=str(tmp_path))
        assert started.is_running is True
        assert "dependency installation FAILED" in started.detail
        assert "the installer exited with status 1" in started.detail
        assert "error: No solution found: nope was not found" in started.detail
        assert tools.repl_eval("1 + 1", cwd=str(tmp_path)).value == "2"
        # The failure is not forgotten: the REPL stays marked as not in sync.
        assert "py.repl_stop()" in tools.repl_start(cwd=str(tmp_path)).detail
    finally:
        tools.repl_stop(cwd=str(tmp_path))
    # Nothing was stamped, so the next start installs again.
    uv.exit_code = 0
    assert environment.prepare(str(tmp_path)).sync.state == "installed"


def test_an_installer_that_outlives_its_limit_is_reported(tmp_path, uv):
    (tmp_path / "pyproject.toml").write_text(PROJECT)
    uv.raises = process.ToolTimeout("uv did not finish within 900s")
    sync = environment.prepare(str(tmp_path)).sync
    assert (sync.state, sync.reason) == (
        "failed",
        "the installer did not finish within 900 s",
    )


def test_the_start_says_what_it_installed_and_then_that_nothing_changed(tmp_path, uv):
    (tmp_path / "requirements.txt").write_text("six\n")
    tools = PythonTools()
    try:
        started = tools.repl_start(cwd=str(tmp_path))
        assert started.detail.startswith("started · dependencies installed with uv in ")
        assert started.command[0] == str(tmp_path / ".venv" / "bin" / "python")
        done = len(uv.commands)
        # A live REPL gets no installation, also when the files changed.
        assert tools.repl_start(cwd=str(tmp_path)).detail == "already running"
        (tmp_path / "requirements.txt").write_text("six\nidna\n")
        again = tools.repl_start(cwd=str(tmp_path))
        assert again.detail.startswith(
            "already running · the project's dependency files changed"
        )
        assert len(uv.commands) == done
    finally:
        tools.repl_stop(cwd=str(tmp_path))
    # The same content again is the content of the last installation.
    (tmp_path / "requirements.txt").write_text("six\n")
    try:
        started = tools.repl_start(cwd=str(tmp_path))
        assert started.detail == "started · dependencies current"
    finally:
        tools.repl_stop(cwd=str(tmp_path))


def test_a_kept_repl_is_found_when_the_files_select_another_interpreter(tmp_path):
    # The REPL started on python3. A `.venv` made later selects another command;
    # a start that did not find the live REPL would replace it and its globals.
    tools, cwd = PythonTools(), str(tmp_path)
    try:
        first = repl.start({"cwd": cwd})
        tools.repl_eval("kept = 41", cwd=cwd)
        repl.detach_all()
        python = tmp_path / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        again = repl.start({"cwd": cwd})
        assert (again["result"], again["pid"]) == ("already-running", first["pid"])
        assert tools.repl_eval("kept + 1", cwd=cwd).value == "42"
    finally:
        tools.repl_stop(cwd=cwd)


def test_shared_packages_get_the_dependencies_and_no_environment(tmp_path, monkeypatch):
    fake = Installer(tmp_path)
    monkeypatch.setattr(environment, "_run", fake)
    monkeypatch.setattr(environment.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setenv("VIS_HOME", str(tmp_path / "vis"))
    (tmp_path / "requirements.txt").write_text("six\n")
    ready = environment.prepare(str(tmp_path), shared=True)
    assert ready.python == ("/bin/vis-agent", "python", "--shared")
    (tmp_path / "pyproject.toml").write_text(PROJECT)
    environment.prepare(str(tmp_path), shared=True)
    assert [" ".join(command) for command in fake.commands] == [
        "/bin/vis-agent python --shared uv pip install -r requirements.txt",
        # `uv sync` would write a `uv.lock` into a project that uv does not own.
        "/bin/vis-agent python --shared uv pip install -r pyproject.toml",
    ]
    (tmp_path / "uv.lock").write_text("")
    environment.prepare(str(tmp_path), shared=True)
    assert fake.commands[-1][-1] == "sync"
    assert not (tmp_path / ".venv").exists()
    done = len(fake.commands)
    assert environment.prepare(str(tmp_path), shared=True).sync.state == "current"
    # Another tool changed shared packages: this project is checked again.
    (tmp_path / "vis" / "python" / "packages" / "other-1.0.dist-info").mkdir(
        parents=True
    )
    assert environment.prepare(str(tmp_path), shared=True).sync.state == "installed"
    assert len(fake.commands) == done + 1


def test_the_uv_of_vis_comes_before_the_uv_on_path(tmp_path, monkeypatch):
    found = {"vis-agent": "/opt/vis-agent", "uv": "/usr/bin/uv"}
    monkeypatch.setattr(environment.shutil, "which", found.get)
    assert environment.installer()[0] == ("/opt/vis-agent", "python", "uv")
    del found["vis-agent"]
    assert environment.installer()[0] == ("uv",)
    assert environment.installer(shared=True)[0] == ()
    del found["uv"]
    (tmp_path / "requirements.txt").write_text("six\n")
    sync = environment.prepare(str(tmp_path)).sync
    assert environment.describe(sync) == (
        "dependencies NOT installed: neither vis-agent nor uv is on PATH"
    )


def _wheel(directory: Path) -> Path:
    """A wheel that needs no index and no build."""
    path = directory / "vis478_dep-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("vis478_dep.py", "ANSWER = 42\n")
        wheel.writestr(
            "vis478_dep-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: vis478-dep\nVersion: 1.0\n",
        )
        wheel.writestr(
            "vis478_dep-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr("vis478_dep-1.0.dist-info/RECORD", "")
    return path


@pytest.fixture
def real_uv(monkeypatch):
    """The uv on PATH, without the seconds that `vis-agent` takes to start."""
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv is not on PATH")
    which = shutil.which
    monkeypatch.setattr(
        environment.shutil,
        "which",
        lambda name: None if name == "vis-agent" else which(name),
    )
    monkeypatch.setenv("UV_OFFLINE", "1")


@pytest.mark.parametrize("kind", ["pyproject", "requirements"])
def test_a_real_start_installs_and_imports_the_dependency(tmp_path, real_uv, kind):
    project = tmp_path / "app"
    project.mkdir()
    wheel = _wheel(tmp_path)
    if kind == "pyproject":
        (project / "pyproject.toml").write_text(
            "[project]\nname = 'app'\nversion = '0'\nrequires-python = '>=3.9'\n"
            f"dependencies = ['vis478-dep @ {wheel.as_uri()}']\n"
        )
    else:
        (project / "requirements.txt").write_text(f"{wheel}\n")
    tools, cwd = PythonTools(), str(project)
    try:
        started = tools.repl_start(cwd=cwd)
        assert "dependencies installed with uv from PATH" in started.detail
        assert (
            os.path.realpath(started.command[0]).startswith(
                os.path.realpath(sys.base_prefix)
            )
            or (project / ".venv").is_dir()
        )
        assert (
            tools.repl_eval("import vis478_dep; vis478_dep.ANSWER", cwd=cwd).value
            == "42"
        )
        tools.repl_stop(cwd=cwd)
        assert tools.repl_start(cwd=cwd).detail == "started · dependencies current"
        assert (
            tools.repl_eval("import vis478_dep; vis478_dep.ANSWER", cwd=cwd).value
            == "42"
        )
    finally:
        tools.repl_stop(cwd=cwd)
