"""Argument handling that does not need a toolchain installed."""

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
