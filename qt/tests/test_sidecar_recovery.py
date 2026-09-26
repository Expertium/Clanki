# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""What the GUI does after the backend replaced a damaged sidecar database
(spec database.sidecar-recovery)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import aqt.utils
from anki.collection_pb2 import SidecarRecoveryResponse
from aqt import fsrs_predictions, rwkv_scheduler, sidecar_recovery


class _Backend:
    def __init__(self, recovery: SidecarRecoveryResponse) -> None:
        self._recovery = recovery

    def take_sidecar_recovery(self) -> SidecarRecoveryResponse:
        # the backend clears the report when it is read
        recovery, self._recovery = self._recovery, SidecarRecoveryResponse()
        return recovery


def _mw(recovery: SidecarRecoveryResponse) -> Any:
    return SimpleNamespace(
        col=SimpleNamespace(_backend=_Backend(recovery)),
        pm=SimpleNamespace(profile={fsrs_predictions.LAST_PASS_DAY_KEY: 123}),
    )


@pytest.fixture
def tooltips(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    shown: list[str] = []
    monkeypatch.setattr(
        aqt.utils, "tooltip", lambda text, *args, **kwargs: shown.append(text)
    )
    monkeypatch.setattr(
        aqt.utils,
        "tr",
        SimpleNamespace(database_check_scheduler_records_lost=lambda: "lost"),
    )
    return shown


def test_a_replaced_cache_restarts_the_passes(
    monkeypatch: pytest.MonkeyPatch, tooltips: list[str]
) -> None:
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_recordings_known_current", True)
    mw = _mw(SidecarRecoveryResponse(cache_replaced=True, moved_aside=["x"]))

    sidecar_recovery.take_and_apply(mw, notify=True)

    # today's FSRS-7 pass is no longer done, and the RWKV rows are counted
    # again the next time a screen asks for them
    assert fsrs_predictions.LAST_PASS_DAY_KEY not in mw.pm.profile
    assert rwkv_scheduler._rwkv_recordings_known_current is False
    # nothing was lost, so nothing is said
    assert tooltips == []


def test_a_replaced_copy_leaves_the_passes_alone(
    monkeypatch: pytest.MonkeyPatch, tooltips: list[str]
) -> None:
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_recordings_known_current", True)
    mw = _mw(SidecarRecoveryResponse(record_copy_replaced=True))

    sidecar_recovery.take_and_apply(mw, notify=True)

    assert mw.pm.profile == {fsrs_predictions.LAST_PASS_DAY_KEY: 123}
    assert rwkv_scheduler._rwkv_recordings_known_current is True
    assert tooltips == []


def test_lost_records_are_said_only_when_lost(tooltips: list[str]) -> None:
    lost = SidecarRecoveryResponse(
        cache_replaced=True, record_copy_replaced=True, records_lost=True
    )

    # at open
    mw = _mw(lost)
    sidecar_recovery.take_and_apply(mw, notify=True)
    assert tooltips == ["lost"]
    # the report was taken: a second read says nothing
    sidecar_recovery.take_and_apply(mw, notify=True)
    assert tooltips == ["lost"]

    # after Check Database, whose own report already says it
    sidecar_recovery.take_and_apply(_mw(lost), notify=False)
    assert tooltips == ["lost"]

    # nothing replaced
    sidecar_recovery.take_and_apply(_mw(SidecarRecoveryResponse()), notify=True)
    assert tooltips == ["lost"]


def test_no_collection_no_report() -> None:
    assert (
        sidecar_recovery.take_and_apply(SimpleNamespace(col=None), notify=True) is None
    )
