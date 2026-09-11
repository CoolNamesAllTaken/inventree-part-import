import io
from pathlib import Path

import pytest

from inventree_part_import import config


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh, empty configuration directory with no configuration loaded."""
    directory = tmp_path / "config"
    monkeypatch.setattr(config, "_config_dir", None)
    monkeypatch.setattr(config, "_config_loaded", None)
    monkeypatch.delenv(config.CONFIG_DIR_ENVIRONMENT_VARIABLE, raising=False)
    config.set_config_dir(directory)
    return directory


@pytest.fixture
def no_terminal(monkeypatch: pytest.MonkeyPatch):
    """stdin that is not a terminal, as under a service or a pipe."""
    monkeypatch.setattr("sys.stdin", io.StringIO())
