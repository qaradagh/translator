"""CLI contract tests.

The launcher menu invokes the CLI by writing out command lines. Nothing tied
those two together, so a flag could be referenced by the menu while never being
registered on the parser - which is exactly how `--preview` shipped broken:
the code that read it and the batch file that passed it both existed, but
argparse rejected it at the door.

These tests parse every command line the launcher actually uses.
"""

import re
from pathlib import Path

import pytest

from gametrans.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "START-HERE.bat"


def parse(argv):
    return build_parser().parse_args(argv)


# -- the flag that shipped broken -------------------------------------------


def test_translate_accepts_preview():
    args = parse(["translate", "--preview", "hello"])
    assert args.preview is True
    assert args.text == ["hello"]


def test_translate_without_preview_defaults_to_false():
    assert parse(["translate", "hello"]).preview is False


# -- every subcommand the menu can reach ------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["run"],
        ["run", "--stats"],
        ["run", "--show-source"],
        ["run", "--region", "0,0,100,50"],
        ["run", "--fps", "20"],
        ["pick-region"],
        ["monitors"],
        ["settings"],
        ["check"],
        ["models"],
        ["setkey"],
        ["setkey", "gemini"],
        ["setkey", "groq"],
        ["setkey", "anthropic"],
        ["translate", "hello"],
        ["translate", "--preview", "hello"],
        ["bench"],
        ["bench", "--iterations", "5"],
        ["compare-models"],
        ["compare-models", "qwen3:8b", "gemma3:12b"],
        ["compare-models", "--base-url", "http://127.0.0.1:11434/v1"],
    ],
)
def test_documented_invocations_parse(argv):
    parse(argv)


def test_no_subcommand_defaults_to_run():
    assert parse([]).command is None  # cli.main() treats this as "run"


def test_unknown_flag_is_rejected():
    with pytest.raises(SystemExit):
        parse(["translate", "--nonexistent", "hello"])


def test_setkey_rejects_an_unknown_provider():
    with pytest.raises(SystemExit):
        parse(["setkey", "not-a-provider"])


# -- the launcher and the CLI must agree ------------------------------------


def _launcher_invocations():
    """Extract `python -m gametrans ...` command lines from the batch file."""
    if not LAUNCHER.is_file():
        return []

    invocations = []
    # Not anchored to the start of the line: the menu also invokes the CLI from
    # inside `if "%choice%"=="1" python -m gametrans ...` one-liners.
    pattern = re.compile(r"python -m gametrans\s+(.+?)\s*$", re.MULTILINE)
    for raw in pattern.findall(LAUNCHER.read_text(encoding="utf-8")):
        argv = []
        for token in raw.split():
            if token.startswith("%") or token.startswith('"%'):
                # A batch variable such as "%sample%" - substitute a placeholder
                # so the shape of the command line is still checked.
                argv.append("placeholder")
            else:
                argv.append(token.strip('"'))
        invocations.append(argv)
    return invocations


def test_launcher_actually_invokes_the_cli():
    assert _launcher_invocations(), "expected the launcher to call the CLI"


@pytest.mark.parametrize("argv", _launcher_invocations(), ids=lambda a: " ".join(a))
def test_every_launcher_command_line_parses(argv):
    """A menu entry that the parser rejects is a broken button."""
    parse(argv)


def test_launcher_covers_the_main_workflow():
    commands = {argv[0] for argv in _launcher_invocations() if argv}
    for required in (
        "setkey", "check", "pick-region", "run", "translate", "models", "settings"
    ):
        assert required in commands, f"the menu no longer offers {required}"


# -- `check` decides whether the launcher may proceed ------------------------


class _FakeOcrBackend:
    name = "windows"

    def close(self):
        pass


@pytest.fixture
def ready_environment(tmp_path, monkeypatch):
    """A machine with a working OCR engine and one API key configured."""
    import gametrans.cli as cli_module
    import gametrans.ocr as ocr_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.AbTestKey")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    real_create = ocr_module.create_backend

    def fake_create(cfg):
        if cfg.backend == "windows":
            return _FakeOcrBackend()
        return real_create(cfg)

    monkeypatch.setattr(ocr_module, "create_backend", fake_create)
    return cli_module


def test_check_passes_when_only_the_region_is_missing(ready_environment, capsys):
    """A missing region must not block the launcher: starting the app with no
    region opens the picker, so failing here would dead-end the menu."""
    assert ready_environment.main(["check"]) == 0

    output = capsys.readouterr().out
    assert "not set yet" in output
    assert "pick-region" in output


def test_check_fails_when_no_provider_is_configured(tmp_path, monkeypatch):
    import gametrans.cli as cli_module
    import gametrans.ocr as ocr_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(ocr_module, "create_backend", lambda cfg: _FakeOcrBackend())

    assert cli_module.main(["check"]) == 1


def test_check_fails_when_no_ocr_engine_is_available(tmp_path, monkeypatch):
    import gametrans.cli as cli_module
    import gametrans.ocr as ocr_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.AbTestKey")

    def no_ocr(cfg):
        raise RuntimeError("nothing installed")

    monkeypatch.setattr(ocr_module, "create_backend", no_ocr)
    assert cli_module.main(["check"]) == 1


def test_check_reports_where_the_code_is_loaded_from(ready_environment, capsys):
    """Several copies of the project on disk is common; say which one is live."""
    ready_environment.main(["check"])
    assert "running from:" in capsys.readouterr().out
