# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins spec/ui.md#ui.process-name: the launchers start Clanki.exe.

PR #203 wrote the copy but nothing started it, so the process kept the
interpreter's name. PR #222 then made `tools/run.py` hand over with
`os.execv`, which on Windows ends the process and starts a new one, so
whatever launched the app stopped seeing it. These tests pin the choice the
launchers make instead, which is a decision about paths and needs no Windows.
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


def test_a_launcher_starts_the_app_under_its_own_name(tmp_path: Path) -> None:
    scripts = _pyenv(tmp_path)

    chosen = clanki_launch.app_interpreter("win32", str(scripts / "python.exe"))

    assert chosen == str(scripts / clanki_launch.APP_EXE)


def test_the_app_is_not_asked_to_find_itself(tmp_path: Path) -> None:
    scripts = _pyenv(tmp_path)
    app = str(scripts / clanki_launch.APP_EXE)

    assert clanki_launch.app_interpreter("win32", app) == app


def test_a_build_without_the_copy_runs_unchanged(tmp_path: Path) -> None:
    scripts = _pyenv(tmp_path, with_app=False)
    interpreter = str(scripts / "python.exe")

    # the name is cosmetic, so a missing copy is not an error
    assert clanki_launch.app_interpreter("win32", interpreter) == interpreter


def test_other_platforms_are_left_alone(tmp_path: Path) -> None:
    scripts = _pyenv(tmp_path)
    interpreter = str(scripts / "python.exe")

    for platform in ("linux", "darwin"):
        assert clanki_launch.app_interpreter(platform, interpreter) == interpreter


def test_no_launcher_replaces_its_own_process() -> None:
    # On Windows `os.execv` does not replace the process: it starts a new one
    # and ends this one, so a launcher waiting on it stops seeing the app.
    # That broke the e2e harness, whose temporary ANKI_BASE was deleted while
    # the app was still starting. Nothing on the launch path may call it.
    for name in ("run.py", "clanki_launch.py"):
        source = (TOOLS / name).read_text(encoding="utf8")
        assert "execv" not in source, f"{name} replaces its own process"

    launcher = Path(__file__).resolve().parents[1] / "tests" / "launch_anki_for_e2e.py"
    assert "execv" not in launcher.read_text(encoding="utf8")


def test_the_e2e_launcher_starts_the_app_under_its_own_name() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "tests" / "launch_anki_for_e2e.py"
    ).read_text(encoding="utf8")

    # it must not go back to sys.executable, which is the plain interpreter
    assert "app_interpreter_here()" in source
    assert "sys.executable" not in source
