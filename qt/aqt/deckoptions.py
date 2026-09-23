# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import aqt
import aqt.fsrs_predictions
import aqt.main
from anki.cards import Card
from anki.decks import DeckConfigsForUpdate, DeckDict, DeckId
from anki.lang import without_unicode_isolation
from aqt import gui_hooks
from aqt.branding import APP_NAME
from aqt.qt import *
from aqt.qt import sip
from aqt.theme import theme_manager
from aqt.utils import (
    disable_help_button,
    restoreGeom,
    saveGeom,
    tr,
)
from aqt.webview import AnkiWebView, AnkiWebViewKind


class DeckOptionsDialog(QDialog):
    "The new deck configuration screen."

    TITLE = "deckOptions"
    silentlyClose = True

    def __init__(self, mw: aqt.main.AnkiQt, deck: DeckDict) -> None:
        QDialog.__init__(self, mw, Qt.WindowType.Window)
        self.mw = mw
        self._deck = deck
        self._close_event_has_cleaned_up = False
        self._ready = False
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.mw.garbage_collect_on_dialog_finish(self)
        self.setMinimumWidth(400)
        self.setMinimumHeight(500)
        disable_help_button(self)
        restoreGeom(self, self.TITLE, default_size=(800, 800))

        self.web = _web_views.take(self)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.web)
        self.setLayout(layout)
        self.show()
        self.setWindowTitle(
            without_unicode_isolation(tr.actions_options_for(val=self._deck["name"]))
        )

    @property
    def deck_id(self) -> DeckId:
        return DeckId(self._deck["id"])

    def set_ready(self):
        self._ready = True
        gui_hooks.deck_options_did_load(self)

    def closeEvent(self, evt: QCloseEvent | None) -> None:
        if self._close_event_has_cleaned_up or not self._ready:
            return super().closeEvent(evt)
        assert evt is not None
        evt.ignore()
        self.web.eval("anki.deckOptionsPendingChanges();")

    def require_close(self):
        """Close. Ensure the closeEvent is not ignored."""
        self._close_event_has_cleaned_up = True
        self.close()

    def reject(self) -> None:
        self.mw.col.set_wants_abort()
        _web_views.give_back(self.web)
        self.web = None  # type: ignore
        saveGeom(self, self.TITLE)
        QDialog.reject(self)


class _DeckOptionsWebView(AnkiWebView):
    """A deck-options web view. Until a window takes it, it has no parent
    and must never appear as a window of its own."""

    def __init__(self) -> None:
        super().__init__(kind=AnkiWebViewKind.DECK_OPTIONS)
        # the load or switch whose ready signal this view waits for
        self.generation = 0
        self.owner: DeckOptionsDialog | None = None
        # a switched page already has its styling, so it is shown once ready
        self.show_on_ready = False

    def setVisible(self, visible: bool) -> None:
        # every page load ends with show(); a view without a window ignores it
        if visible and self.parentWidget() is None:
            return
        super().setVisible(visible)


class _DeckOptionsWebViews:
    """Keeps one deck-options web view ready in the background, so opening
    the window does not pay for a new web view (and its QtWebEngine
    process) and a first page load.

    The spare view is loaded in full and never shown. Opening the window
    takes it and moves its page to the chosen deck with a client-side
    navigation that runs the page loader again, so the settings always come
    fresh from the collection (`anki.deckOptionsSwitch` in
    ts/routes/deck-options/[deckId]). A spare that has not finished loading
    gets a full load of the chosen deck instead; with no spare at all, the
    window makes its own view, as before.

    Each view serves one window and is destroyed when the window closes, as
    before; a new spare is made a little later. A view is not reused: its
    renderer grows by about 20 MB with every load or switch and gives the
    memory back only near 750 MB, and a fresh page is also one no add-on
    has touched.

    Every load and switch has a new generation number, carried in the page
    URL; the page's ready signal names it (mediasrv reads it from the
    request's referrer), so a late signal from an earlier load is ignored.

    The first spare is made WARM_DELAY_MS after the profile opens, once the
    start-up work is done, never while reviewing or while the FSRS-7
    prediction pass holds the collection; the spare is released when
    the profile closes."""

    WARM_DELAY_MS = 40_000
    WARM_RETRY_MS = 10_000
    REWARM_AFTER_CLOSE_MS = 2_000

    def __init__(self) -> None:
        self._spare: _DeckOptionsWebView | None = None
        self._spare_ready = False
        self._in_use: set[_DeckOptionsWebView] = set()
        self._generation = 0
        self._profile = 0

    # profile life cycle

    def on_profile_did_open(self) -> None:
        self._profile += 1
        self._warm_later(self.WARM_DELAY_MS)

    def on_profile_will_close(self) -> None:
        # a pending warm-up belongs to this profile
        self._profile += 1
        spare, self._spare = self._spare, None
        self._spare_ready = False
        if spare is not None:
            spare.cleanup()
            spare.deleteLater()

    def _warm_later(self, delay_ms: int) -> None:
        profile = self._profile
        aqt.mw.progress.single_shot(delay_ms, lambda: self._warm(profile))

    def _warm(self, profile: int) -> None:
        from aqt import rwkv_scheduler

        mw = aqt.mw
        if profile != self._profile or mw.col is None or self._spare is not None:
            return
        # Stay out of the way of start-up work and of reviewing: making the
        # view costs the main thread a little. The FSRS-7 prediction pass
        # holds the collection for seconds at a time, and the warm-up reads
        # the current deck on the main thread, which would freeze the window
        # until the pass let go (4.4 s measured).
        if (
            rwkv_scheduler.rwkv_state_cache_loading(mw)
            or aqt.fsrs_predictions.is_holding_collection()
            or mw.state not in ("deckBrowser", "overview")
            or mw.app.activeModalWidget() is not None
        ):
            self._warm_later(self.WARM_RETRY_MS)
            return
        self._spare = _DeckOptionsWebView()
        self._spare_ready = False
        self._load(self._spare, DeckId(mw.col.decks.get_current_id()))

    # opening and closing

    def take(self, dialog: DeckOptionsDialog) -> _DeckOptionsWebView:
        web, ready = self._spare, self._spare_ready
        self._spare, self._spare_ready = None, False
        if web is None:
            web = _DeckOptionsWebView()
        web.owner = dialog
        self._in_use.add(web)
        web.hide_while_preserving_layout()
        if ready:
            self._switch(web, dialog.deck_id)
        else:
            self._load(web, dialog.deck_id)
        return web

    def give_back(self, web: _DeckOptionsWebView) -> None:
        self._in_use.discard(web)
        web.owner = None
        web.cleanup()
        if not self._in_use:
            self._warm_later(self.REWARM_AFTER_CLOSE_MS)

    # loads and switches

    def _path(self, web: _DeckOptionsWebView, deck_id: DeckId) -> str:
        self._generation += 1
        web.generation = self._generation
        return f"deck-options/{deck_id}?g={web.generation}"

    def _load(self, web: _DeckOptionsWebView, deck_id: DeckId) -> None:
        web.load_sveltekit_page(self._path(web, deck_id))

    def _switch(self, web: _DeckOptionsWebView, deck_id: DeckId) -> None:
        url = "/" + self._path(web, deck_id)
        if theme_manager.night_mode:
            url += "#night"
        web.show_on_ready = True
        web.eval(f"anki.deckOptionsSwitch({json.dumps(url)});")

    def on_page_ready(self, generation: int | None) -> None:
        """The page of a deck-options view finished loading or switching.
        Without a generation (a page from elsewhere, such as the HMR dev
        server) the ready signal goes to an open window, as before."""
        for web in self._in_use:
            if generation is not None and generation != web.generation:
                continue
            if sip.isdeleted(web) or web.owner is None:
                continue
            if web.show_on_ready:
                web.show_on_ready = False
                web.show()
            web.owner.set_ready()
            return
        spare = self._spare
        if spare is not None and generation == spare.generation:
            self._spare_ready = True


_web_views = _DeckOptionsWebViews()


def on_deck_options_page_ready(referrer: str | None) -> None:
    """Called by mediasrv when a deck-options page says it is ready. The
    page's URL, which is the request's referrer, holds its generation."""
    generation: int | None = None
    if referrer:
        values = parse_qs(urlparse(referrer).query).get("g")
        if values and values[0].isdigit():
            generation = int(values[0])
    _web_views.on_page_ready(generation)


def setup_deck_options_web_views() -> None:
    gui_hooks.profile_did_open.append(_web_views.on_profile_did_open)
    gui_hooks.profile_will_close.append(_web_views.on_profile_will_close)


def confirm_deck_then_display_options(active_card: Card | None = None) -> None:
    decks = [aqt.mw.col.decks.current()]
    if card := active_card:
        if card.odid and card.odid != decks[0]["id"]:
            deck = aqt.mw.col.decks.get(card.odid)
            assert deck is not None
            decks.append(deck)

        if not any(d["id"] == card.did for d in decks):
            deck = aqt.mw.col.decks.get(card.did)
            assert deck is not None
            decks.append(deck)

    if len(decks) == 1:
        display_options_for_deck(decks[0])
    else:
        decks.sort(key=lambda x: x["dyn"])
        _deck_prompt_dialog(decks)


def _deck_prompt_dialog(decks: list[DeckDict]) -> None:
    diag = QDialog(aqt.mw.app.activeWindow())
    diag.setWindowTitle(APP_NAME)
    box = QVBoxLayout()
    box.addWidget(QLabel(tr.deck_config_which_deck()))
    for deck in decks:
        button = QPushButton(deck["name"])
        qconnect(button.clicked, diag.close)
        qconnect(button.clicked, lambda _, deck=deck: display_options_for_deck(deck))
        box.addWidget(button)
    button = QPushButton(tr.actions_cancel())
    qconnect(button.clicked, diag.close)
    box.addWidget(button)
    diag.setLayout(box)
    diag.open()


def display_options_for_deck_id(deck_id: DeckId) -> None:
    deck = aqt.mw.col.decks.get(deck_id)
    assert deck is not None
    display_options_for_deck(deck)


def display_options_for_deck(deck: DeckDict) -> None:
    if not deck["dyn"]:
        # never the old Qt dialog (Shift+click opened it): it shows the SM-2
        # settings, which Clanki has none of (spec deck-options.no-sm2-settings)
        DeckOptionsDialog(aqt.mw, deck)
    else:
        aqt.dialogs.open("FilteredDeckConfigDialog", aqt.mw, deck_id=deck["id"])


# The collection's one scheduling algorithm
######################################################################

SchedulingAlgorithm = DeckConfigsForUpdate.SchedulingAlgorithm


def algorithm_name(algorithm: SchedulingAlgorithm.V) -> str:
    if algorithm == SchedulingAlgorithm.RWKV_CURVE:
        return tr.deck_config_scheduler_choice_rwkv_curve()
    if algorithm == SchedulingAlgorithm.RWKV_INSTANT:
        return tr.deck_config_scheduler_choice_rwkv_instant()
    return tr.deck_config_scheduler_choice_fsrs()


def ask_reschedule_after_algorithm_change(
    parent: QWidget, algorithm: SchedulingAlgorithm.V
) -> bool:
    """Asked when a deck-options save changes the algorithm, except to
    RWKV-Instant, which has no intervals to reschedule (spec
    sched.algorithm-change-prompt). Closing the question keeps the due dates."""
    if algorithm == SchedulingAlgorithm.RWKV_INSTANT:
        return False
    name = algorithm_name(algorithm)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(tr.deck_config_scheduler())
    box.setText(tr.deck_config_algorithm_changed_question(algorithm=name))
    reschedule = box.addButton(
        tr.deck_config_reschedule_all_now(), QMessageBox.ButtonRole.AcceptRole
    )
    keep = box.addButton(
        tr.deck_config_keep_due_dates(), QMessageBox.ButtonRole.RejectRole
    )
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    box.exec()
    return box.clickedButton() is reschedule


def after_algorithm_change(
    mw: aqt.main.AnkiQt, algorithm: SchedulingAlgorithm.V, reschedule: bool
) -> None:
    """Drop RWKV's cached targets and queue scores and refresh the study
    screens; then, if the user chose it, reschedule every card."""
    from aqt import rwkv_scheduler
    from aqt.operations import CollectionOp

    rwkv_scheduler.rwkv_instant_retention_did_change(mw)
    if not reschedule:
        return
    if algorithm == SchedulingAlgorithm.FSRS7:
        CollectionOp(
            mw, lambda col: col._backend.reschedule_all_cards_with_fsrs7()
        ).run_in_background()
    elif algorithm == SchedulingAlgorithm.RWKV_CURVE:
        rwkv_scheduler.reschedule_rwkv_review_cards_with_progress(mw)
