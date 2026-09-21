# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Which interpreter starts the app, so the process is named Clanki.

Task Manager shows a process's description resource, not its file name, and
the interpreter's description says "Python". The build writes a copy of the
interpreter whose description says Clanki (``tools/win_app_exe.py``), and
every launcher starts the app with that copy. See spec/ui.md#ui.process-name.

The choice lives here, in a module of its own, so a launcher can make it
without importing the app, and so a test can exercise it on any platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: written beside the interpreter, inside the virtual environment, because
#: the interpreter finds pyvenv.cfg beside itself and starts nowhere else
APP_EXE = "Clanki.exe"


def app_interpreter(platform: str, executable: str) -> str:
    """The interpreter to start the app with.

    It is the Clanki-named copy when the build has written one beside
    `executable`, and `executable` itself otherwise: another platform, or a
    build that has not written the copy yet. Neither is an error, because
    the name is cosmetic and the app is not.
    """
    if platform != "win32":
        return executable
    interpreter = Path(executable)
    if interpreter.name.lower() == APP_EXE.lower():
        return executable
    app = interpreter.with_name(APP_EXE)
    return str(app) if app.exists() else executable


def app_interpreter_here() -> str:
    """`app_interpreter` for the interpreter running this code."""
    return app_interpreter(sys.platform, sys.executable)
