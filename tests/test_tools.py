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
