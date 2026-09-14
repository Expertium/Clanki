# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import hashlib
import os
from collections.abc import MutableMapping
from pathlib import Path

PORTABLE_MARKER = "anki-portable"
PORTABLE_DATA_DIR = "Anki Portable Data"


def configure_portable_environment(
    module_file: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> Path | None:
    """Point a marked app bundle at data stored beside the app."""
    module_file = (module_file or Path(__file__)).resolve()
    environ = environ if environ is not None else os.environ

    if len(module_file.parents) < 3:
        return None

    resources = module_file.parents[2]
    if not (resources / PORTABLE_MARKER).is_file():
        return None

    if resources.name == "Resources" and resources.parent.name == "Contents":
        portable_root = resources.parents[2]
    else:
        portable_root = resources

    data_dir = portable_root / PORTABLE_DATA_DIR
    temp_dir = data_dir / ".tmp"
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeError(
            "Anki Portable cannot create its data folder because the application "
            "is in a read-only or otherwise unwritable location. Move the entire "
            "'Anki Portable' folder to a writable location, then open Anki Portable "
            "again. On macOS, move the folder in Finder before the first launch so "
            "Gatekeeper does not run the app from a read-only temporary location."
        ) from error

    root_digest = hashlib.sha256(os.fsencode(portable_root)).hexdigest()[:16]
    environ["ANKI_BASE"] = str(data_dir)
    environ["ANKI_PORTABLE"] = "1"
    environ["ANKI_PORTABLE_ROOT"] = str(portable_root)
    environ["ANKI_SINGLE_INSTANCE_KEY"] = f"anki-portable-{root_digest}"
    for variable in ("TMPDIR", "TMP", "TEMP"):
        environ[variable] = str(temp_dir)
    return data_dir


def main():
    configure_portable_environment()

    import aqt

    aqt.run()
