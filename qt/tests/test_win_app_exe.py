# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins spec/ui.md#ui.process-name."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="the process name is a Windows resource"
)


def _module():
    import win_app_exe

    return win_app_exe


def _interpreter() -> Path:
    windowless = Path(sys.executable).with_name("pythonw.exe")
    return windowless if windowless.exists() else Path(sys.executable)


def test_the_copy_describes_itself_as_the_app(tmp_path: Path) -> None:
    module = _module()
    target = module.make_app_exe(_interpreter(), tmp_path / "Clanki.exe")
    resource, _language = module._read_version_resource(target)

    assert module._utf16_value(module.APP_NAME) in resource
    assert module._utf16_value(module.INTERPRETER_NAME) not in resource


def test_the_copy_is_the_same_size_and_still_runs(tmp_path: Path) -> None:
    module = _module()
    interpreter = _interpreter()
    target = module.make_app_exe(interpreter, tmp_path / "Clanki.exe")

    # the name is written in place, so nothing else in the file moves
    assert target.stat().st_size == interpreter.stat().st_size

    # the copy runs the interpreter only from inside the virtual
    # environment, where pyvenv.cfg sits beside it, which is where the build
    # writes it
    console = Path(sys.executable).with_name("ClankiTestCopy.exe")
    module.make_app_exe(Path(sys.executable), console)
    try:
        done = subprocess.run(
            [str(console), "-c", "import sys; print(sys.prefix)"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        console.unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == sys.prefix
