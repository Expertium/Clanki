# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Hand a source run over to Clanki.exe, so the process is named Clanki.

Task Manager shows a process's description resource, not its file name, and
the interpreter's description says "Python". The build writes a copy of the
interpreter whose description says Clanki (``tools/win_app_exe.py``); this
module hands over to that copy. See spec/ui.md#ui.process-name.

It is a module of its own, and not part of ``tools/run.py``, so that a test
can exercise the decision without importing the app.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

#: the copy carries this, so it does not hand over to itself for ever
ALREADY_LAUNCHED = "CLANKI_LAUNCHED"

#: written beside the interpreter, inside the virtual environment
APP_EXE = "Clanki.exe"


def handover_command(
    platform: str,
    executable: str,
    argv: Sequence[str],
    environ: Mapping[str, str],
) -> list[str] | None:
    """The command to start instead, or None to carry on as we are.

    Four things stop the hand-over: another platform, a run that already
    handed over, a run that is already the copy, and a build that has not
    written the copy yet.
    """
    if platform != "win32" or environ.get(ALREADY_LAUNCHED):
        return None
    interpreter = Path(executable)
    app = interpreter.with_name(APP_EXE)
    if interpreter.name.lower() == APP_EXE.lower():
        return None
    if not app.exists():
        return None
    return [str(app), *argv]


def run_as_the_app() -> None:
    """Start again as Clanki.exe, before the app itself is imported.

    The interpreter that steps aside has done nothing but start, so the cost
    is one bare interpreter, not a second copy of the app's imports.
    """
    command = handover_command(sys.platform, sys.executable, sys.argv, os.environ)
    if command is None:
        return
    os.environ[ALREADY_LAUNCHED] = "1"
    try:
        os.execv(command[0], command)
    except OSError:
        # the copy cannot be started: the name is cosmetic, the app is not
        pass
