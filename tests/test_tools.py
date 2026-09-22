"""Argument handling that does not need a toolchain installed."""

from pathlib import Path

import pytest

from vis_lang_python import tools


def test_the_project_root_is_the_nearest_marker(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    nested = tmp_path / "src" / "app"
    nested.mkdir(parents=True)
    assert tools._root("", (str(nested),)) == str(tmp_path.resolve())


def test_an_unmarked_directory_is_its_own_root(tmp_path):
    assert tools._root(str(tmp_path)) == str(tmp_path.resolve())


def test_formatting_needs_python_files(tmp_path):
    (tmp_path / "notes.txt").write_text("hello")
    with pytest.raises(ValueError, match="no Python file"):
        tools.PythonTools().format_code([str(tmp_path)])


def test_linting_needs_python_files(tmp_path):
    with pytest.raises(ValueError, match="no Python file"):
        tools.PythonTools().lint_code([str(tmp_path)])


def test_relative_paths_are_resolved_against_cwd(tmp_path, monkeypatch):
    # Regression: `cwd` named the project while relative `paths` were read from
    # Vis' own process directory, so a project elsewhere failed with
    # FileNotFoundError instead of being formatted.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "thing.py").write_text("x = 1\n")
    seen = {}

    def remember(files, root, **options):
        seen["files"] = list(files)
        seen["root"] = root
        return "formatted"

    monkeypatch.setattr(tools.ruff_tool, "format_files", remember)
    monkeypatch.chdir(tmp_path.parent)
    assert tools.PythonTools().format_code(["pkg/thing.py"], cwd=str(tmp_path))
    assert seen["files"] == [(package / "thing.py").resolve()]
    assert seen["root"] == str(tmp_path.resolve())


def test_absolute_paths_are_left_alone(tmp_path):
    absolute = str(tmp_path / "thing.py")
    assert tools._in_root(str(tmp_path), (absolute,)) == (absolute,)


def _hosted_in(monkeypatch, session, spawns=None):
    """Pretend Vis hosts this call, with its shell running in `session`."""
    monkeypatch.setattr(tools, "_SESSION_ROOT", {})
    monkeypatch.setattr(tools.process, "is_hosted", lambda: True)
    monkeypatch.setattr(
        tools.process, "tool_path", lambda name, hint="": f"/bin/{name}"
    )

    def answered(command, **options):
        if spawns is not None:
            spawns.append(tuple(command))
        return tools.process.ToolRun(tuple(command), 0, f"{session}\n", "", 1)

    monkeypatch.setattr(tools.process, "run", answered)


def test_a_relative_cwd_is_the_session_and_not_the_vis_install(tmp_path, monkeypatch):
    # Regression for Blockether/vis#280: `cwd="."` and an omitted `cwd` named
    # Vis' own installation directory, so a project elsewhere answered with a
    # missing pytest and with files that were not there.
    session = tmp_path / "project"
    (session / "pkg").mkdir(parents=True)
    (session / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    (session / "pkg" / "thing.py").write_text("x = 1\n")
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.chdir(install)
    _hosted_in(monkeypatch, session)
    root = str(session.resolve())

    assert tools._root(".") == root
    assert tools._root("") == root
    assert tools._root("", ("pkg/thing.py",)) == root
    assert tools._in_root(root, ("pkg/thing.py",)) == (
        str(session.resolve() / "pkg" / "thing.py"),
    )


def test_a_relative_repl_cwd_is_the_session_project(tmp_path, monkeypatch):
    # The same regression on the REPL surface: a relative `cwd` asked about an
    # interpreter in Vis' installation directory.
    session = tmp_path / "project"
    session.mkdir()
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.chdir(install)
    _hosted_in(monkeypatch, session)

    assert tools.PythonTools().repl_status(cwd=".").directory == str(session.resolve())


def test_the_session_directory_is_asked_for_once(tmp_path, monkeypatch):
    spawns = []
    _hosted_in(monkeypatch, tmp_path, spawns)

    assert tools.session_root() == str(tmp_path)
    assert tools.session_root() == str(tmp_path)
    assert len(spawns) == 1


def test_a_host_that_cannot_answer_leaves_the_process_directory(tmp_path, monkeypatch):
    def refused(command, **options):
        raise RuntimeError("the jail refused this spawn")

    monkeypatch.setattr(tools, "_SESSION_ROOT", {})
    monkeypatch.setattr(tools.process, "is_hosted", lambda: True)
    monkeypatch.setattr(
        tools.process, "tool_path", lambda name, hint="": f"/bin/{name}"
    )
    monkeypatch.setattr(tools.process, "run", refused)
    monkeypatch.chdir(tmp_path)

    assert tools.session_root() == str(Path.cwd())
