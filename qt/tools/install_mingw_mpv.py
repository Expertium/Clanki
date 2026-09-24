# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Put the MinGW build of mpv into anki_audio/mingw (spec ui.audio-mingw-mpv).

anki-audio 0.2.3 from PyPI ships mpv v0.41.0's MSVC build, which deadlocks
during start-up when the machine is busy: with 14 cores kept busy it hung in
31 of 40 starts, and the nightly MSVC build in 23 of 40. The MinGW build of
the same release (same commit, 41f6a6450) hung in 0 of 40, and so did the
nightly MinGW build. A hung start costs the user their sound until Clanki
restarts, so Windows x64 builds carry the MinGW build, and aqt.sound prefers
it. The MinGW mpv.exe needs the DLLs that come with it, so they are copied
too; the console launcher (mpv.com) and the file-association scripts are not.

The files go into a folder of their own and anki-audio's files are never
written: uv hard-links an installed package's files to its cache, so writing
over anki-audio's mpv.exe would change every environment that shares it (and
fails while any of them runs mpv). A file that is already in place is not
copied again, so a rebuild works while Clanki is open.

    python qt/tools/install_mingw_mpv.py <MinGW mpv.exe> <stamp file>

The anki_audio folder is found by importing anki_audio, so run it with the
interpreter of the environment to change (the build uses out/pyenv's).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# the folder inside anki_audio; aqt.sound._packagedCmd looks here first
MINGW_FOLDER = "mingw"


def mingw_mpv_files(mingw_dir: Path) -> list[Path]:
    """mpv.exe and the DLLs it loads, from the extracted MinGW build."""
    files = sorted(
        path
        for path in mingw_dir.iterdir()
        if path.is_file() and (path.name == "mpv.exe" or path.suffix.lower() == ".dll")
    )
    if not any(path.name == "mpv.exe" for path in files):
        raise FileNotFoundError(f"no mpv.exe in {mingw_dir}")
    return files


def _same_file(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return False
    a, b = src.stat(), dst.stat()
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def install_mingw_mpv(mingw_dir: Path, audio_dir: Path) -> list[str]:
    """Copy the MinGW mpv into audio_dir/mingw; returns the names in place."""
    files = mingw_mpv_files(mingw_dir)
    target = audio_dir / MINGW_FOLDER
    target.mkdir(exist_ok=True)
    for path in files:
        dst = target / path.name
        if _same_file(path, dst):
            continue
        try:
            shutil.copy2(path, dst)
        except PermissionError as err:
            raise PermissionError(
                f"cannot replace {dst}: close Clanki and build again"
            ) from err
    return [path.name for path in files]


def main(argv: list[str]) -> None:
    mpv_exe, stamp = Path(argv[1]), Path(argv[2])
    import anki_audio

    audio_dir = Path(anki_audio.__file__).parent
    names = install_mingw_mpv(mpv_exe.parent, audio_dir)
    print(f"MinGW mpv: {len(names)} files in {audio_dir / MINGW_FOLDER}")
    stamp.write_text("", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv)
