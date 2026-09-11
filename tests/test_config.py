import os
import subprocess
import sys
from pathlib import Path

import pytest

from inventree_part_import import config
from inventree_part_import.exceptions import ConfigurationError


def test_importing_the_package_creates_nothing(tmp_path: Path):
    """
    The configuration directory used to be created (and a .gitignore written) at import
    time. Checked in a subprocess because this process has long since imported the package.
    """
    directory = tmp_path / "never-created"
    script = (
        "import os, sys\n"
        "import inventree_part_import.config\n"
        "import inventree_part_import.suppliers\n"
        f"print(os.path.exists({str(directory)!r}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, config.CONFIG_DIR_ENVIRONMENT_VARIABLE: str(directory)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"


def test_the_environment_variable_picks_the_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    directory = tmp_path / "from-env"
    monkeypatch.setattr(config, "_config_dir", None)
    monkeypatch.setenv(config.CONFIG_DIR_ENVIRONMENT_VARIABLE, str(directory))

    assert config.get_config_dir() == directory.resolve()
    assert directory.is_dir()
    assert (directory / ".gitignore").read_text() == "inventree.yaml\nsuppliers.yaml\n"


def test_set_config_dir_wins_over_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(config, "_config_dir", None)
    monkeypatch.setenv(config.CONFIG_DIR_ENVIRONMENT_VARIABLE, str(tmp_path / "env"))
    config.set_config_dir(tmp_path / "explicit")

    assert config.get_config_dir() == (tmp_path / "explicit").resolve()


def test_set_config_replaces_the_file(config_dir: Path):
    effective = config.set_config({"currency": "EUR", "language": "de", "location": "DE"})

    assert effective["currency"] == "EUR"
    assert effective["interactive"] == config.DEFAULT_CONFIG_VARS["interactive"]
    assert config.get_config() is effective
    assert not (config_dir / config.CONFIG).exists(), "nothing is written"


def test_set_config_refuses_unknown_parameters(config_dir: Path):
    with pytest.raises(ConfigurationError, match="unknown configuration parameter"):
        config.set_config({"currency": "EUR", "colour": "blue"})


def test_a_missing_config_without_a_terminal_is_an_error(config_dir: Path, no_terminal: None):
    with pytest.raises(ConfigurationError, match="set_config"):
        config.get_config()


def test_an_existing_config_file_is_still_read(config_dir: Path, no_terminal: None):
    (config_dir / config.CONFIG).write_text("currency: GBP\nlanguage: en\nlocation: GB\n")

    assert config.get_config()["currency"] == "GBP"
