"""Tests of other behaviour install nothing: only `installs` tests reach an installer."""

import pytest

from vis_lang_python import environment


@pytest.fixture(autouse=True)
def dependency_installation(request, tmp_path_factory, monkeypatch):
    # The stamps of a test never reach the person's own cache.
    monkeypatch.setenv("VIS_LANG_PYTHON_CACHE", str(tmp_path_factory.mktemp("stamps")))
    if "installs" not in request.keywords:
        monkeypatch.setattr(environment, "_plan", lambda root, shared: None)
