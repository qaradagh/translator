"""`.env` parsing, precedence, and in-place key editing."""

import os

import pytest

from gametrans.dotenv import (
    env_file_path,
    find_env_file,
    load_dotenv,
    parse_env_text,
    set_key,
    source_of,
)


# -- parsing ----------------------------------------------------------------


def test_parses_simple_pairs():
    assert parse_env_text("A=1\nB=two\n") == {"A": "1", "B": "two"}


def test_ignores_comments_and_blank_lines():
    text = "# a comment\n\nA=1\n   \n#B=2\n"
    assert parse_env_text(text) == {"A": "1"}


def test_strips_quotes_and_export_prefix():
    text = 'export A="quoted"\nB=\'single\'\nC=bare\n'
    assert parse_env_text(text) == {"A": "quoted", "B": "single", "C": "bare"}


def test_strips_trailing_comment_from_unquoted_values():
    assert parse_env_text("A=value # note\n") == {"A": "value"}
    # ...but a '#' inside a quoted value is part of the key.
    assert parse_env_text('A="val#ue"\n') == {"A": "val#ue"}


def test_keeps_special_characters_common_in_api_keys():
    text = "GEMINI_API_KEY=AIzaSy-Abc_123.xyz\nGROQ_API_KEY=gsk_aB3/x+Y=\n"
    parsed = parse_env_text(text)
    assert parsed["GEMINI_API_KEY"] == "AIzaSy-Abc_123.xyz"
    assert parsed["GROQ_API_KEY"] == "gsk_aB3/x+Y="


def test_empty_value_is_kept_as_empty():
    assert parse_env_text("GEMINI_API_KEY=\n") == {"GEMINI_API_KEY": ""}


def test_lines_without_an_equals_sign_are_skipped():
    assert parse_env_text("just some text\nA=1\n") == {"A": "1"}


# -- loading ----------------------------------------------------------------


def test_load_sets_variables(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_KEY_A", raising=False)
    (tmp_path / ".env").write_text("TEST_KEY_A=from-file\n", encoding="utf-8")

    applied = load_dotenv()
    assert applied == {"TEST_KEY_A": "from-file"}
    assert os.environ["TEST_KEY_A"] == "from-file"
    assert source_of("TEST_KEY_A") == ".env"


def test_existing_environment_wins_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_KEY_B", "from-environment")
    (tmp_path / ".env").write_text("TEST_KEY_B=from-file\n", encoding="utf-8")

    applied = load_dotenv()
    assert applied == {}
    assert os.environ["TEST_KEY_B"] == "from-environment"


def test_override_flag_lets_the_file_win(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_KEY_C", "from-environment")
    (tmp_path / ".env").write_text("TEST_KEY_C=from-file\n", encoding="utf-8")

    load_dotenv(override=True)
    assert os.environ["TEST_KEY_C"] == "from-file"


def test_missing_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_dotenv() == {}
    assert find_env_file() is None


def test_env_is_found_next_to_the_config_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_KEY_D", raising=False)

    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("TEST_KEY_D=beside-config\n", encoding="utf-8")

    load_dotenv(str(project / "config.toml"))
    assert os.environ["TEST_KEY_D"] == "beside-config"


def test_bom_prefixed_file_still_parses(tmp_path, monkeypatch):
    """Notepad on Windows writes UTF-8 with a BOM."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_KEY_E", raising=False)
    (tmp_path / ".env").write_bytes(b"\xef\xbb\xbfTEST_KEY_E=works\n")

    load_dotenv()
    assert os.environ["TEST_KEY_E"] == "works"


def test_source_of_reports_the_plain_environment(monkeypatch):
    monkeypatch.setenv("TEST_KEY_F", "value")
    assert source_of("TEST_KEY_F") == "environment"
    monkeypatch.delenv("TEST_KEY_F")
    assert source_of("TEST_KEY_F") == ""


# -- writing ----------------------------------------------------------------


def test_set_key_creates_the_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = set_key("GEMINI_API_KEY", "AIzaNEW")
    assert path.read_text(encoding="utf-8").strip() == "GEMINI_API_KEY=AIzaNEW"
    assert os.environ["GEMINI_API_KEY"] == "AIzaNEW"


def test_set_key_replaces_without_disturbing_the_rest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "# my notes\nGEMINI_API_KEY=old\nGROQ_API_KEY=gsk_keepme\n", encoding="utf-8"
    )

    set_key("GEMINI_API_KEY", "AIzaNEW")
    body = (tmp_path / ".env").read_text(encoding="utf-8")

    assert "GEMINI_API_KEY=AIzaNEW" in body
    assert "GEMINI_API_KEY=old" not in body
    assert "GROQ_API_KEY=gsk_keepme" in body   # untouched
    assert "# my notes" in body                # comments preserved
    assert body.count("GEMINI_API_KEY") == 1   # not duplicated


def test_set_key_appends_a_new_variable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=AIzaX\n", encoding="utf-8")

    set_key("GROQ_API_KEY", "gsk_new")
    parsed = parse_env_text((tmp_path / ".env").read_text(encoding="utf-8"))
    assert parsed == {"GEMINI_API_KEY": "AIzaX", "GROQ_API_KEY": "gsk_new"}


def test_set_key_does_not_uncomment_a_commented_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("# GROQ_API_KEY=disabled\n", encoding="utf-8")

    set_key("GROQ_API_KEY", "gsk_real")
    body = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "# GROQ_API_KEY=disabled" in body
    assert "GROQ_API_KEY=gsk_real" in body


def test_written_key_round_trips_through_the_parser(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tricky = "gsk_aB3/x+Y=zzz"
    set_key("GROQ_API_KEY", tricky)
    parsed = parse_env_text((tmp_path / ".env").read_text(encoding="utf-8"))
    assert parsed["GROQ_API_KEY"] == tricky


def test_env_file_path_defaults_to_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert env_file_path() == tmp_path / ".env"


def test_load_config_picks_up_the_env_file(tmp_path, monkeypatch):
    """The whole point: a key in .env is visible to the provider chain."""
    from gametrans.config import load_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=AIzaFromFile\n", encoding="utf-8")
    (tmp_path / "config.toml").write_text("[capture]\ntarget_fps = 10\n", encoding="utf-8")

    load_config("config.toml")
    assert os.environ["GEMINI_API_KEY"] == "AIzaFromFile"
