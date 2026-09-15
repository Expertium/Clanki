# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/scheduling.md#sched.advance through pylib: the request, the
# preview and the move reach the backend and their answers come back.

from anki.cards_pb2 import FsrsMemoryState
from anki.consts import CARD_TYPE_REV, QUEUE_TYPE_REV
from anki.scheduler.base import AdvancePostponeRequest
from tests.shared import getEmptyCol


def test_advance_through_the_backend() -> None:
    col = getEmptyCol()
    col.set_config("schedulingAlgorithm", "fsrs7")
    note = col.newNote()
    note["Front"] = "one"
    col.addNote(note)
    card = note.cards()[0]
    card.type = CARD_TYPE_REV
    card.queue = QUEUE_TYPE_REV
    card.ivl = 20
    card.due = col.sched.today + 1
    card.memory_state = FsrsMemoryState(stability=20, difficulty=5)
    col.update_card(card)

    request = AdvancePostponeRequest(mode=AdvancePostponeRequest.ADVANCE)
    candidates = col.sched.advance_postpone_candidates(request)
    assert list(candidates.card_ids) == [card.id]
    assert not candidates.rwkv_curve
    preview = col.sched.preview_advance_postpone(request)
    assert list(preview.card_ids) == [card.id]
    assert preview.retrievability_after[0] > preview.retrievability_before[0]

    response = col.sched.advance_postpone(
        AdvancePostponeRequest(mode=AdvancePostponeRequest.ADVANCE, card_ids=[card.id])
    )
    assert response.count == 1
    assert response.changes.card
    card.load()
    # 19 days after its last review (its due date minus its interval)
    assert (card.due, card.ivl) == (col.sched.today, 19)
    assert col.undo_status().undo == "Advance Cards"
    assert col.db.scalar("select count() from revlog") == 0
