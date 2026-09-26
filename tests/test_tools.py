"""Argument handling that does not need a toolchain installed."""

import runpy
from pathlib import Path

import blockether.vis.extension as vis
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


def test_a_relative_cwd_is_the_session_and_not_the_vis_install(tmp_path, monkeypatch):
    # Blockether/vis#280: neither `cwd="."` nor an omitted cwd names Vis' install.
    session = tmp_path / "project"
    (session / "pkg").mkdir(parents=True)
    (session / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    (session / "pkg" / "thing.py").write_text("x = 1\n")
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.chdir(install)
    language = tools.PythonTools(workspace_root=lambda: session)
    root = str(session.resolve())

    assert language._root(".") == root
    assert language._root("") == root
    assert language._root("", ("pkg/thing.py",)) == root
    assert tools._in_root(root, ("pkg/thing.py",)) == (
        str(session.resolve() / "pkg" / "thing.py"),
    )


def test_a_relative_repl_cwd_is_the_session_project(tmp_path, monkeypatch):
    # Blockether/vis#280: status checks must target the session's interpreter.
    session = tmp_path / "project"
    session.mkdir()
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.chdir(install)
    language = tools.PythonTools(workspace_root=lambda: session)

    assert language.repl_status(cwd=".").directory == str(session.resolve())


def test_outside_tools_use_the_process_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert tools._absolute("thing.py") == Path.cwd() / "thing.py"
    assert tools._root(".") == str(Path.cwd())


def test_a_missing_host_root_does_not_silently_use_the_install(tmp_path, monkeypatch):
    # Blockether/vis#280: a failed host lookup must not redirect tools to Vis' code.
    monkeypatch.chdir(tmp_path)

    def unavailable():
        raise RuntimeError("no session workspace")

    language = tools.PythonTools(workspace_root=unavailable)
    with pytest.raises(RuntimeError, match="no session workspace"):
        language._root(".")


def test_draft_switch_updates_relative_format_tests_and_repl(tmp_path, monkeypatch):
    # Blockether/vis#280: an extension must follow the live working copy, not a cached `pwd`.
    roots = (tmp_path / "source", tmp_path / "draft")
    for root in roots:
        (root / "pkg").mkdir(parents=True)
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
        (root / "pkg" / "thing.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    current = {"root": roots[0]}
    language = tools.PythonTools(workspace_root=lambda: current["root"])
    monkeypatch.setattr(
        tools.ruff_tool,
        "format_files",
        lambda files, root, **options: (tuple(files), root),
    )
    monkeypatch.setattr(
        tools.pytest_tool, "run", lambda paths, *, root, **options: root
    )
    monkeypatch.setattr(
        tools.repl, "status", lambda request: {"cwd": request["cwd"], "status": "down"}
    )

    for root in roots:
        current["root"] = root
        files, selected = language.format_code(["pkg/thing.py"], cwd=".")
        assert files == ((root / "pkg" / "thing.py").resolve(),)
        assert selected == str(root.resolve())
        assert language.run_tests(cwd=".") == str(root.resolve())
        assert language.repl_status(cwd=".").directory == str(root.resolve())


def test_entrypoint_binds_the_live_sdk_workspace_root(tmp_path, monkeypatch):
    # Blockether/vis#280: registration must pass the SDK door, not a sampled path.
    roots = (tmp_path / "source", tmp_path / "draft")
    for root in roots:
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    current = {"root": roots[0]}
    registered = []
    monkeypatch.setattr(vis, "workspace_root", lambda: current["root"])
    monkeypatch.setattr(vis, "register_extension", registered.append)

    runpy.run_path(str(Path(__file__).resolve().parents[1] / "extension.py"))
    assert len(registered) == 1
    language = registered[0].symbols[0].fn
    for root in roots:
        current["root"] = root
        assert language._root(".") == str(root.resolve())


def _entrypoint_tags(monkeypatch):
    registered = []
    monkeypatch.setattr(vis, "register_extension", registered.append)
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "extension.py"))
    members = registered[0].symbols[0].contract["members"]
    return {member["name"].rsplit(".", 1)[-1]: member["tag"] for member in members}


def _older_host(monkeypatch):
    """Stand in for a Vis host that knows only observation and mutation tags."""
    method = vis.method

    def older(fn=None, *, tag="observation", **options):
        if tag not in ("observation", "mutation"):
            raise ValueError(
                f"vis.method tag must be observation or mutation, got {tag!r}"
            )
        return method(fn, tag=tag, **options)

    monkeypatch.setattr(vis, "method", older)


def _knows_verification():
    try:
        vis.method(tag="verification")
    except ValueError:
        return False
    return True


@pytest.mark.skipif(
    not _knows_verification(), reason="this Vis SDK predates check tags"
)
def test_lint_and_test_runs_report_as_checks(monkeypatch):
    tags = _entrypoint_tags(monkeypatch)
    assert (tags["lint_code"], tags["run_tests"]) == ("verification", "verification")
    assert tags["format_code"] == "mutation"


def test_an_older_host_records_lint_and_test_runs_as_reads(monkeypatch):
    _older_host(monkeypatch)
    tags = _entrypoint_tags(monkeypatch)
    assert (tags["lint_code"], tags["run_tests"]) == ("observation", "observation")
    assert tags["repl_eval"] == "mutation"
