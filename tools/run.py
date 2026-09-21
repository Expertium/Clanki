# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import os
import sys

# This script does not choose the interpreter it runs under. The launchers
# do, so that the process is named Clanki (spec ui.process-name): replacing
# this process here would hide the app from whatever started it.

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
