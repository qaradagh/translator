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
    for required in ("setkey", "check", "pick-region", "run", "translate", "models"):
        assert required in commands, f"the menu no longer offers {required}"
