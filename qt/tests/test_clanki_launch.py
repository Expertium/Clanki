# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins spec/ui.md#ui.process-name: the source run hands over to Clanki.exe.

PR #203 wrote the copy but nothing started it, so the process kept the
interpreter's name. These tests pin the hand-over itself, which is a
decision about paths and environment variables and needs no Windows.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

import clanki_launch  # noqa: E402


def _pyenv(tmp_path: Path, *, with_app: bool = True) -> Path:
    """A virtual environment's Scripts directory, with or without the copy."""
    scripts = tmp_path / "pyenv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"")
    if with_app:
        (scripts / clanki_launch.APP_EXE).write_bytes(b"")
    return scripts


def test_a_source_run_starts_again_as_the_app(tmp_path: Path) -> None:
    scripts = _pyenv(tmp_path)

    command = clanki_launch.handover_command(
        "win32",
        str(scripts / "python.exe"),
        ["tools/run.py", "-b", "base"],
        {},
    )

    assert command == [
        str(scripts / clanki_launch.APP_EXE),
        "tools/run.py",
        "-b",
        "base",
    ]


def test_the_app_does_not_hand_over_to_itself(tmp_path: Path) -> None:
    scripts = _pyenv(tmp_path)

    # the copy itself, and then any run that already handed over
    assert (
        clanki_launch.handover_command(
            "win32", str(scripts / clanki_launch.APP_EXE), ["tools/run.py"], {}
        )
        is None
    )
    assert (
        clanki_launch.handover_command(
            "win32",
            str(scripts / "python.exe"),
            ["tools/run.py"],
            {clanki_launch.ALREADY_LAUNCHED: "1"},
        )
        is None
    )


def test_a_build_without_the_copy_runs_unchanged(tmp_path: Path) -> None:
    scripts = _pyenv(tmp_path, with_app=False)

    # the name is cosmetic, so a missing copy is not an error
    assert (
        clanki_launch.handover_command(
            "win32", str(scripts / "python.exe"), ["tools/run.py"], {}
        )
        is None
    )


def test_other_platforms_are_left_alone(tmp_path: Path) -> None:
    scripts = _pyenv(tmp_path)

    for platform in ("linux", "darwin"):
        assert (
            clanki_launch.handover_command(
                platform, str(scripts / "python.exe"), ["tools/run.py"], {}
            )
            is None
        )


def test_run_py_hands_over_before_it_imports_the_app() -> None:
    lines = (TOOLS / "run.py").read_text(encoding="utf8").splitlines()

    # the interpreter that steps aside must have done nothing but start
    assert lines.index("run_as_the_app()") < lines.index("import aqt")
