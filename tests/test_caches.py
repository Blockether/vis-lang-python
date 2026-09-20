"""A confined run is handed the caches the person's own tools already fill."""

from pathlib import Path

from vis_lang_python import caches

VARIABLES = (
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "POETRY_CACHE_DIR",
    "PIP_CACHE_DIR",
)


def _without_overrides(monkeypatch):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_the_defaults_are_the_ones_a_person_already_has(monkeypatch):
    _without_overrides(monkeypatch)
    home = Path.home()
    assert caches.uv_cache() == str(home / ".cache" / "uv")
    assert caches.uv_interpreters() == str(home / ".local" / "share" / "uv" / "python")
    assert caches.poetry_cache() == str(home / ".cache" / "pypoetry")
    assert caches.pip_cache() == str(home / ".cache" / "pip")


def test_a_machine_that_keeps_them_elsewhere_is_followed(monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", "/srv/uv")
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", "/srv/pythons")
    monkeypatch.setenv("POETRY_CACHE_DIR", "/srv/poetry")
    monkeypatch.setenv("PIP_CACHE_DIR", "/srv/pip")
    assert caches.granted_paths() == (
        "/srv/uv",
        "/srv/pythons",
        "/srv/poetry",
        "/srv/pip",
    )


def test_a_call_may_add_the_directory_it_writes_to(monkeypatch):
    _without_overrides(monkeypatch)
    granted = caches.granted_paths("/tmp/report-7")
    assert granted[-1] == "/tmp/report-7"
    assert caches.uv_cache() in granted
