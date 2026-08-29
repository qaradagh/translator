import os
import tempfile

import pytest

from gametrans.config import (
    AppConfig,
    RegionConfig,
    default_provider_chain,
    load_config,
    save_region,
)


def write(tmpdir, text, name="config.toml"):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_defaults_load_without_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert isinstance(cfg, AppConfig)
    assert cfg.capture.target_fps == 12
    assert [p.name for p in cfg.translate.providers] == [
        p.name for p in default_provider_chain()
    ]


def test_missing_explicit_path_is_an_error():
    with pytest.raises(FileNotFoundError):
        load_config("/definitely/not/here.toml")


def test_sections_override_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        path = write(
            tmp,
            """
            log_level = "DEBUG"

            [region]
            left = 100
            top = 900
            width = 1720
            height = 150

            [capture]
            target_fps = 20

            [ocr]
            backend = "rapidocr"
            upscale = 1.5

            [overlay]
            font_size = 30
            show_source = true
            """,
        )
        cfg = load_config(path)

    assert cfg.log_level == "DEBUG"
    assert cfg.region.as_tuple() == (100, 900, 1720, 150)
    assert cfg.region.is_set is True
    assert cfg.capture.target_fps == 20
    assert cfg.ocr.backend == "rapidocr"
    assert cfg.ocr.upscale == 1.5
    assert cfg.overlay.font_size == 30
    assert cfg.overlay.show_source is True


def test_custom_provider_chain_replaces_the_default():
    with tempfile.TemporaryDirectory() as tmp:
        path = write(
            tmp,
            """
            [translate]
            target_language = "Persian (Farsi)"
            concurrency = 5

            [[translate.providers]]
            name = "groq-only"
            kind = "openai"
            model = "llama-3.3-70b-versatile"
            api_key_env = "GROQ_API_KEY"
            base_url = "https://api.groq.com/openai/v1"
            rpm_limit = 30

            [[translate.providers]]
            name = "local"
            kind = "openai"
            model = "qwen3:8b"
            base_url = "http://127.0.0.1:11434/v1"
            enabled = false
            """,
        )
        cfg = load_config(path)

    assert cfg.translate.concurrency == 5
    assert [p.name for p in cfg.translate.providers] == ["groq-only", "local"]
    assert cfg.translate.providers[0].rpm_limit == 30
    assert cfg.translate.providers[1].enabled is False


def test_glossary_and_context_survive_loading():
    with tempfile.TemporaryDirectory() as tmp:
        path = write(
            tmp,
            """
            [translate]
            context_hint = "Elden Ring, dark fantasy"

            [translate.glossary]
            "Site of Grace" = "جایگاه فیض"
            """,
        )
        cfg = load_config(path)

    assert cfg.translate.context_hint == "Elden Ring, dark fantasy"
    assert cfg.translate.glossary["Site of Grace"] == "جایگاه فیض"


def test_unknown_keys_are_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        path = write(tmp, "[capture]\ntarget_fps = 15\nnot_a_real_key = 3\n")
        cfg = load_config(path)
    assert cfg.capture.target_fps == 15


def test_env_override_for_region(monkeypatch):
    monkeypatch.setenv("GAMETRANS_REGION", "10,20,300,40")
    with tempfile.TemporaryDirectory() as tmp:
        path = write(tmp, "[capture]\ntarget_fps = 10\n")
        cfg = load_config(path)
    assert cfg.region.as_tuple() == (10, 20, 300, 40)


def test_malformed_env_region_is_ignored(monkeypatch):
    monkeypatch.setenv("GAMETRANS_REGION", "not,a,region")
    with tempfile.TemporaryDirectory() as tmp:
        path = write(tmp, "")
        cfg = load_config(path)
    assert cfg.region.is_set is False


def test_save_region_creates_the_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.toml")
        save_region(path, RegionConfig(left=1, top=2, width=3, height=4, monitor=2))
        cfg = load_config(path)
    assert cfg.region.as_tuple() == (1, 2, 3, 4)
    assert cfg.region.monitor == 2


def test_save_region_replaces_the_block_and_keeps_the_rest():
    with tempfile.TemporaryDirectory() as tmp:
        path = write(
            tmp,
            """
            # my notes
            [capture]
            target_fps = 25

            [region]
            left = 0
            top = 0
            width = 10
            height = 10

            [overlay]
            font_size = 40
            """,
        )
        save_region(path, RegionConfig(left=5, top=6, width=700, height=80))
        cfg = load_config(path)
        body = open(path, encoding="utf-8").read()

    assert cfg.region.as_tuple() == (5, 6, 700, 80)
    assert cfg.capture.target_fps == 25   # untouched
    assert cfg.overlay.font_size == 40    # untouched
    assert "# my notes" in body           # comments preserved
    assert body.count("[region]") == 1    # not duplicated
