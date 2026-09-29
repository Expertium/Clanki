# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins the .colpkg export half of spec/sync.md#sync.full-upload-keeps-rwkv-state."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import anki.lang

anki.lang.set_lang("en")

from aqt import rwkv_scheduler  # noqa: E402
from aqt.import_export import exporting  # noqa: E402


class _QueryOp:
    """Runs the op at once, then its success or failure callback, as the
    real QueryOp does on the collection thread and then the main thread."""

    ops: list[_QueryOp] = []

    def __init__(self, *, parent: Any, op: Any, success: Any) -> None:
        self.op = op
        self.success = success
        self.on_failure: Any = None
        _QueryOp.ops.append(self)

    def with_backend_progress(self, _update: Any) -> _QueryOp:
        return self

    def failure(self, on_failure: Any) -> _QueryOp:
        self.on_failure = on_failure
        return self

    def run_in_background(self) -> None:
        pass


def _options() -> exporting.ExportOptions:
    return exporting.ExportOptions(
        out_path="out.colpkg",
        include_scheduling=True,
        include_deck_configs=True,
        include_media=False,
        include_tags=True,
        include_html=True,
        include_deck=True,
        include_notetype=True,
        include_guid=True,
        legacy_support=False,
        limit=None,
        parent=None,
    )


@pytest.mark.parametrize("fails", [False, True])
def test_a_colpkg_export_stops_the_passes_and_keeps_the_rwkv_state(
    monkeypatch, fails: bool
) -> None:
    """The export closes the collection and opens it again, as a full upload
    does: the RWKV and FSRS-7 passes stop before the close, and after the
    reopen (also after a failed export) the resident state stays or is
    restored without a window, and the passes may run again."""
    order: list[str] = []
    _QueryOp.ops = []
    monkeypatch.setattr(exporting, "QueryOp", _QueryOp)
    monkeypatch.setattr(
        exporting.gui_hooks,
        "exporter_will_export",
        lambda options, _exporter: options,
    )
    monkeypatch.setattr(
        exporting.gui_hooks,
        "collection_will_temporarily_close",
        lambda _col: order.append("screens let go of the collection"),
    )
    monkeypatch.setattr(
        exporting.gui_hooks, "exporter_did_export", lambda _options, _exporter: None
    )
    monkeypatch.setattr(
        exporting, "_show_exported_tooltip", lambda *_args: order.append("tooltip")
    )
    monkeypatch.setattr(
        exporting, "show_exception", lambda **_kwargs: order.append("error shown")
    )
    monkeypatch.setattr(
        rwkv_scheduler, "begin_full_sync", lambda _mw: order.append("stop passes")
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "wait_for_background_work_before_full_sync",
        lambda _col: order.append("wait for passes"),
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "collection_package_exported",
        lambda _mw: order.append("keep or restore the state, passes may run"),
    )
    col = SimpleNamespace(
        export_collection_package=lambda *_args, **_kwargs: order.append(
            "close and export"
        )
    )
    mw = SimpleNamespace(col=col, reopen=lambda: order.append("reopen"))

    exporting.ColpkgExporter().export(mw, _options())  # type: ignore[arg-type]
    op = _QueryOp.ops[0]
    op.op(col)
    if fails:
        op.on_failure(Exception("disk full"))
    else:
        op.success(None)

    assert order == [
        "screens let go of the collection",
        "stop passes",
        "wait for passes",
        "close and export",
        "reopen",
        "keep or restore the state, passes may run",
        "error shown" if fails else "tooltip",
    ]
