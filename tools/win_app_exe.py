# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Make a Windows executable that Task Manager calls "Clanki".

A source build runs `pythonw.exe`, so Task Manager groups the windows under
"Python": it shows a process's FileDescription resource, not its file name,
and a renamed copy of the interpreter keeps the interpreter's description.

This copies the interpreter and rewrites that one string in place. "Python"
and "Clanki" are both six characters, so the file keeps its exact size: every
length field in the version resource stays correct, and so does everything
else in the file.

The string is written in place, not through the Windows resource API
(BeginUpdateResource), which rebuilds the executable. A rebuild can drop
whatever follows the image, and the interpreter in a uv virtual environment
is a launcher that may keep its own data there. In place, the only bytes
that change are the twelve of the name.

    python tools/win_app_exe.py [out/pyenv/Scripts/Clanki.exe]
"""

from __future__ import annotations

import ctypes
import shutil
import sys
from ctypes import wintypes
from pathlib import Path

APP_NAME = "Clanki"
INTERPRETER_NAME = "Python"

RT_VERSION = 16
VERSION_RESOURCE_ID = 1
LOAD_LIBRARY_AS_DATAFILE = 0x00000002
NEUTRAL_LANGUAGES = (0x0409, 0x0000, 0x0400)


def _utf16_value(text: str) -> bytes:
    """The bytes of one NUL-terminated UTF-16 value inside the resource."""
    return text.encode("utf-16-le") + b"\x00\x00"


def _kernel32() -> ctypes.WinDLL:
    """kernel32 with the argument types spelled out.

    Without them ctypes truncates the 64-bit handles and raises
    "int too long to convert" on the first call that takes one back.
    """
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.LoadLibraryExW.restype = wintypes.HMODULE
    dll.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    dll.FreeLibrary.argtypes = [wintypes.HMODULE]
    dll.FindResourceExW.restype = wintypes.HANDLE
    dll.FindResourceExW.argtypes = [
        wintypes.HMODULE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.WORD,
    ]
    dll.SizeofResource.restype = wintypes.DWORD
    dll.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    dll.LoadResource.restype = wintypes.HANDLE
    dll.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
    dll.LockResource.restype = ctypes.c_void_p
    dll.LockResource.argtypes = [wintypes.HANDLE]
    return dll


def _read_version_resource(exe: Path) -> tuple[bytes, int]:
    """Return the file's version resource and the language it is stored under."""
    kernel32 = _kernel32()
    module = kernel32.LoadLibraryExW(str(exe), None, LOAD_LIBRARY_AS_DATAFILE)
    if not module:
        raise OSError(ctypes.get_last_error(), f"cannot open {exe}")
    try:
        for language in NEUTRAL_LANGUAGES:
            found = kernel32.FindResourceExW(
                module,
                ctypes.c_void_p(RT_VERSION),
                ctypes.c_void_p(VERSION_RESOURCE_ID),
                language,
            )
            if found:
                break
        else:
            raise OSError(f"{exe} has no version resource")
        size = kernel32.SizeofResource(module, found)
        loaded = kernel32.LoadResource(module, found)
        address = kernel32.LockResource(loaded)
        return ctypes.string_at(address, size), language
    finally:
        kernel32.FreeLibrary(module)


def _patch_in_place(exe: Path, resource: bytes, old: bytes, new: bytes) -> None:
    """Replace `old` with `new` inside the file's own copy of `resource`.

    Both strings have the same length, so nothing else in the file moves.
    """
    if len(old) != len(new):
        raise ValueError("the replacement must have the same length")
    image = exe.read_bytes()
    start = image.find(resource)
    if start < 0:
        raise OSError(f"cannot find the version resource inside {exe}")
    if image.find(resource, start + 1) >= 0:
        raise OSError(f"the version resource of {exe} is not unique")
    patched = resource.replace(old, new)
    exe.write_bytes(image[:start] + patched + image[start + len(resource) :])


def make_app_exe(interpreter: Path, target: Path) -> Path:
    """Copy the interpreter to `target` and name it after the app."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(interpreter, target)
    resource, _language = _read_version_resource(target)
    old = _utf16_value(INTERPRETER_NAME)
    new = _utf16_value(APP_NAME)
    if old not in resource:
        raise SystemExit(
            f"{interpreter} does not describe itself as {INTERPRETER_NAME}"
        )
    # every occurrence of the whole value, never a longer string that starts
    # with it, such as the company name "Python Software Foundation"
    _patch_in_place(target, resource, old, new)
    return target


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("this script only makes sense on Windows")
    # the interpreter that runs this script, so the build needs no path
    scripts = Path(sys.executable).parent
    interpreter = scripts / "pythonw.exe"
    if not interpreter.exists():
        raise SystemExit(f"{interpreter} does not exist; build pyenv first")
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else scripts / f"{APP_NAME}.exe"
    made = make_app_exe(interpreter, target)
    print(f"wrote {made}")


if __name__ == "__main__":
    main()
