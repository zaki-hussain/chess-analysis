from pathlib import Path

import pytest

from chess_analysis.config import ConfigError, find_config, load_settings


def write_config(directory: Path, text: str) -> Path:
    path = directory / "config.toml"
    path.write_text(text)
    return path


def test_username_comes_from_config(tmp_path):
    config = write_config(tmp_path, 'username = "SomePlayer"\n')
    settings = load_settings(config_path=config)
    assert settings.username == "someplayer"
    assert settings.data_dir == tmp_path / "data"
    assert settings.raw_dir == tmp_path / "data" / "someplayer" / "raw"


def test_command_line_username_beats_config(tmp_path):
    config = write_config(tmp_path, 'username = "someplayer"\n')
    assert load_settings("Other_Player", config_path=config).username == "other_player"


def test_data_dir_is_relative_to_config_file(tmp_path):
    config = write_config(tmp_path, 'username = "a"\ndata_dir = "elsewhere"\n')
    assert load_settings(config_path=config).data_dir == tmp_path / "elsewhere"


def test_missing_username_is_an_error(tmp_path):
    config = write_config(tmp_path, 'data_dir = "data"\n')
    with pytest.raises(ConfigError, match="No chess.com username"):
        load_settings(config_path=config)


@pytest.mark.parametrize("bad", ["../etc", "has space", "a/b", ""])
def test_unsafe_usernames_are_rejected(tmp_path, bad):
    config = write_config(tmp_path, 'username = "fallback"\n')
    with pytest.raises(ConfigError):
        load_settings(bad or " ", config_path=config)


def test_config_is_found_in_a_parent_directory(tmp_path):
    config = write_config(tmp_path, 'username = "a"\n')
    nested = tmp_path / "notebooks" / "deeper"
    nested.mkdir(parents=True)
    assert find_config(nested) == config
