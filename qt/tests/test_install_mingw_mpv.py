# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from pathlib import Path

import pytest
from tools.install_mingw_mpv import install_mingw_mpv


def _mingw_build(folder: Path) -> Path:
    folder.mkdir()
    for name in ("mpv.exe", "libstdc++-6.dll", "avcodec-62.dll", "vulkan-1.dll"):
        (folder / name).write_text(f"mingw {name}", encoding="utf-8")
    # not needed to play sound
    for name in ("mpv.com", "mpv-register.bat", "mpv-unregister.bat"):
        (folder / name).write_text(name, encoding="utf-8")
    return folder


# Pins spec/ui.md#ui.audio-mingw-mpv
def test_the_mingw_mpv_goes_beside_anki_audios_files(tmp_path: Path) -> None:
    mingw = _mingw_build(tmp_path / "mpv_mingw")
    audio = tmp_path / "anki_audio"
    audio.mkdir()
    (audio / "mpv.exe").write_text("msvc mpv.exe", encoding="utf-8")
    (audio / "vulkan-1.dll").write_text("msvc vulkan-1.dll", encoding="utf-8")

    names = install_mingw_mpv(mingw, audio)

    assert names == ["avcodec-62.dll", "libstdc++-6.dll", "mpv.exe", "vulkan-1.dll"]
    target = audio / "mingw"
    assert (target / "mpv.exe").read_text(encoding="utf-8") == "mingw mpv.exe"
    assert (target / "vulkan-1.dll").read_text(encoding="utf-8") == "mingw vulkan-1.dll"
    # anki-audio's own files are never written: uv hard-links them to its cache
    assert (audio / "mpv.exe").read_text(encoding="utf-8") == "msvc mpv.exe"
    assert (audio / "vulkan-1.dll").read_text(encoding="utf-8") == "msvc vulkan-1.dll"
    # the console launcher and the scripts are not copied
    assert sorted(p.name for p in target.iterdir()) == names


# Pins spec/ui.md#ui.audio-mingw-mpv
def test_files_already_in_place_are_not_copied_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mingw = _mingw_build(tmp_path / "mpv_mingw")
    audio = tmp_path / "anki_audio"
    audio.mkdir()
    install_mingw_mpv(mingw, audio)

    def locked(src: object, dst: object) -> None:
        raise PermissionError("in use")

    # a running Clanki holds mpv.exe open; a rebuild must still pass
    monkeypatch.setattr("tools.install_mingw_mpv.shutil.copy2", locked)
    assert "mpv.exe" in install_mingw_mpv(mingw, audio)

    # a changed file that cannot be written names the fix
    (mingw / "mpv.exe").write_text("a newer mpv.exe", encoding="utf-8")
    with pytest.raises(PermissionError, match="close Clanki"):
        install_mingw_mpv(mingw, audio)


def test_a_folder_without_mpv_is_an_error(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    (folder / "zlib1.dll").touch()
    audio = tmp_path / "anki_audio"
    audio.mkdir()

    with pytest.raises(FileNotFoundError, match="no mpv.exe"):
        install_mingw_mpv(folder, audio)
    assert not (audio / "mingw").exists()
