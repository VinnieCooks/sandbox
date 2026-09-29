import json

import pytest

from ops.settings import load_settings, read_env_file


def test_precedence_and_empty_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "A=file\nB=file\nC=file\n"
        'export D="quoted # not a comment"\n'
        "E=value # comment\n"
        "F=   # only a comment\n"
        "#G=commented out\n"
        "not a setting\n",
        encoding="utf-8",
    )
    environ = {"A": "env", "B": "", "OPS_SECRETS_JSON": json.dumps({"B": "secret", "C": "secret", "H": "", "N": 5})}
    settings = load_settings(environ, env_file)
    assert settings["A"] == "env"            # the process environment wins
    assert settings["B"] == "secret"         # an empty env value does not mask a secret
    assert settings["C"] == "secret"         # secrets beat the env file
    assert settings["D"] == "quoted # not a comment"
    assert settings["E"] == "value"
    for absent in ("F", "G", "H", "N", "OPS_SECRETS_JSON"):
        assert absent not in settings


def test_missing_env_file_is_fine(tmp_path):
    assert load_settings({"X": "1"}, tmp_path / "nope") == {"X": "1"}


def test_invalid_secrets_json_exits():
    with pytest.raises(SystemExit, match="OPS_SECRETS_JSON"):
        load_settings({"OPS_SECRETS_JSON": "{nope"})


def test_read_env_file_single_quotes(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TOKEN='abc def'\n", encoding="utf-8")
    assert read_env_file(path) == {"TOKEN": "abc def"}
