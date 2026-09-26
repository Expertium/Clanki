# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""What the GUI does after the backend replaced a damaged sidecar database
beside the collection (spec database.sidecar-recovery).

The backend does the recovery itself, when the collection opens and in Check
Database: it moves the damaged file aside, makes a new one and restores the
record of which algorithm scheduled each review from its copy. What is left
for the GUI:

- a new retrievability cache holds no predictions, so the passes that write
  them must not think they are done: the FSRS-7 prediction pass forgets that
  it finished today, and the RWKV recording pass forgets that the rows were
  current. Both then run the way they always do, in the background, when
  their usual trigger comes (spec ui.stats-fsrs-predictions-ready,
  sched.rwkv-recordings-automatic);
- the user hears of it only when records were really lost.
"""

from __future__ import annotations

import logging
from typing import Any

from anki.collection_pb2 import SidecarRecoveryResponse

logger = logging.getLogger(__name__)


def take_and_apply(mw: Any, *, notify: bool) -> SidecarRecoveryResponse | None:
    """Reads (and clears) what the backend did, and acts on it. `notify` is
    False after Check Database, whose own report already says it."""
    col = getattr(mw, "col", None)
    if col is None:
        return None
    try:
        recovery = col._backend.take_sidecar_recovery()
    except Exception:
        logger.exception("could not read the sidecar recovery report")
        return None
    apply(mw, recovery, notify=notify)
    return recovery


def apply(mw: Any, recovery: SidecarRecoveryResponse, *, notify: bool) -> None:
    if not (recovery.cache_replaced or recovery.record_copy_replaced):
        return
    logger.warning(
        "replaced a damaged sidecar database: cache=%s record copy=%s "
        "records lost=%s, moved aside: %s",
        recovery.cache_replaced,
        recovery.record_copy_replaced,
        recovery.records_lost,
        ", ".join(recovery.moved_aside) or "nothing",
    )
    if recovery.cache_replaced:
        _forget_that_the_predictions_are_ready(mw)
    if notify and recovery.records_lost:
        from aqt.utils import tooltip, tr

        tooltip(tr.database_check_scheduler_records_lost(), period=10000, parent=mw)


def _forget_that_the_predictions_are_ready(mw: Any) -> None:
    from aqt import fsrs_predictions, rwkv_scheduler

    profile = getattr(getattr(mw, "pm", None), "profile", None)
    if isinstance(profile, dict):
        profile.pop(fsrs_predictions.LAST_PASS_DAY_KEY, None)
    rwkv_scheduler._forget_that_the_recordings_are_current()
