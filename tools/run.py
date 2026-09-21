# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import os
import sys
from pathlib import Path

# The process must be named Clanki before anything else happens, so this
# comes before `import aqt` (spec ui.process-name).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from clanki_launch import run_as_the_app  # noqa: E402

run_as_the_app()

sys.path.extend(["pylib", "qt", "out/pylib", "out/qt"])

import aqt

# A source run has no launcher, so aqt never sets a Windows AppUserModelID and
# the taskbar identifies the window as pythonw.exe: "Pin to taskbar" pins
# Python and the pinned icon launches nothing. Give the dev build its own
# identity; the desktop shortcut carries the same ID (see CLAUDE.md).
if sys.platform == "win32":
    try:
        from win32com.shell import shell

        shell.SetCurrentProcessExplicitAppUserModelID("Expertium.Clanki.Dev")
    except Exception:  # pragma: no cover - cosmetic only
        pass

if not os.environ.get("SKIP_RUN"):
    aqt.run()
