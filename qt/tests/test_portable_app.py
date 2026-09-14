# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def load_portable_app_module() -> ModuleType:
    path = Path("qt/installer/app/src/anki/app.py")
    spec = importlib.util.spec_from_file_location("anki_portable_app", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def portable_module(root: Path, layout: str = "macos") -> Path:
    if layout == "macos":
        root = root / "Anki Portable.app" / "Contents" / "Resources"
    return root / "app" / "anki" / "app.pyc"


def test_unmarked_app_does_not_change_environment(tmp_path: Path) -> None:
    module = load_portable_app_module()
    module_file = portable_module(tmp_path)
    module_file.parent.mkdir(parents=True)
    environ = {"ANKI_BASE": "existing"}

    assert module.configure_portable_environment(module_file, environ) is None
    assert environ == {"ANKI_BASE": "existing"}


@pytest.mark.parametrize("layout", ["macos", "windows", "linux"])
def test_portable_app_uses_adjacent_isolated_data(tmp_path: Path, layout: str) -> None:
    module = load_portable_app_module()
    module_file = portable_module(tmp_path, layout)
    resources = module_file.parents[2]
    resources.mkdir(parents=True, exist_ok=True)
    (resources / module.PORTABLE_MARKER).touch()
    environ = {
        "ANKI_BASE": "/main/Anki2",
        "ANKI_SINGLE_INSTANCE_KEY": "anki-main",
        "TMPDIR": "/tmp",
    }

    data_dir = module.configure_portable_environment(module_file, environ)

    assert data_dir == tmp_path / module.PORTABLE_DATA_DIR
    assert data_dir.is_dir()
    assert (data_dir / ".tmp").is_dir()
    assert environ["ANKI_BASE"] == str(data_dir)
    assert environ["ANKI_PORTABLE"] == "1"
    assert environ["ANKI_PORTABLE_ROOT"] == str(tmp_path)
    assert environ["ANKI_SINGLE_INSTANCE_KEY"].startswith("anki-portable-")
    for variable in ("TMPDIR", "TMP", "TEMP"):
        assert environ[variable] == str(data_dir / ".tmp")


def test_each_portable_folder_gets_a_distinct_instance_key(tmp_path: Path) -> None:
    module = load_portable_app_module()
    keys = []
    for folder in (tmp_path / "one", tmp_path / "two"):
        module_file = portable_module(folder, "linux")
        resources = module_file.parents[2]
        resources.mkdir(parents=True)
        (resources / module.PORTABLE_MARKER).touch()
        environ: dict[str, str] = {}
        module.configure_portable_environment(module_file, environ)
        keys.append(environ["ANKI_SINGLE_INSTANCE_KEY"])

    assert keys[0] != keys[1]


def test_unwritable_portable_location_has_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_portable_app_module()
    module_file = portable_module(tmp_path)
    resources = module_file.parents[2]
    resources.mkdir(parents=True)
    (resources / module.PORTABLE_MARKER).touch()
    environ = {"ANKI_BASE": "/main/Anki2"}

    def fail_to_create_temp_dir(*_args: object, **_kwargs: object) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", fail_to_create_temp_dir)

    with pytest.raises(RuntimeError, match="Move the entire 'Anki Portable' folder"):
        module.configure_portable_environment(module_file, environ)

    assert environ == {"ANKI_BASE": "/main/Anki2"}
