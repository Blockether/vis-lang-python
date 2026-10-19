"""A confined run may execute the toolchain that the person installed at home."""

import os
import runpy
from pathlib import Path

import blockether.vis.extension as vis

from vis_lang_python import toolchain

ENTRYPOINT = Path(__file__).resolve().parents[1] / "extension.py"


def _program(directory, name):
    path = directory / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_a_program_and_the_file_it_links_to_are_granted(tmp_path, monkeypatch):
    (tmp_path / "install").mkdir()
    real = _program(tmp_path / "install", "vis-agent-0.2")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "vis-agent").symlink_to(real)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert toolchain.toolchain_paths() == (
        str(bin_dir / "vis-agent"),
        os.path.realpath(real),
    )


def test_no_program_on_path_gives_no_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert toolchain.toolchain_paths() == ()


def test_a_grant_the_host_would_refuse_is_left_out(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".vis").mkdir(parents=True)
    tool = _program(home, "uv")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VIS_HOME", raising=False)
    assert toolchain.grantable(
        [
            "/",
            str(home),
            str(home / ".vis"),
            str(tmp_path),
            "relative",
            str(tool),
            str(tool),
        ]
    ) == (str(tool),)


def _declared(monkeypatch):
    """The keyword arguments the entrypoint gives `vis.Extension`."""
    registered = []
    monkeypatch.setattr(vis, "Extension", lambda **declaration: declaration)
    monkeypatch.setattr(vis, "register_extension", registered.append)
    runpy.run_path(str(ENTRYPOINT))
    return registered[0]


def test_a_host_with_grants_receives_the_toolchain(tmp_path, monkeypatch):
    tool = _program(tmp_path, "uv")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(
        vis, "Grant", lambda path, *, reason: ("grant", path, reason), raising=False
    )
    grants = _declared(monkeypatch)["grants"]
    assert [grant[1] for grant in grants] == [str(tool)]
    assert all(grant[2] for grant in grants)


def test_an_older_host_registers_without_grants(tmp_path, monkeypatch):
    _program(tmp_path, "uv")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delattr(vis, "Grant", raising=False)
    assert "grants" not in _declared(monkeypatch)
