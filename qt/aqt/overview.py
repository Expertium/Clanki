# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aqt
import aqt.operations
import aqt.rwkv_scheduler
import aqt.ui_split
from anki.collection import Collection, OpChanges
from anki.scheduler import UnburyDeck
from aqt import gui_hooks
from aqt.deckdescription import DeckDescriptionDialog
from aqt.deckoptions import display_options_for_deck
from aqt.operations import QueryOp
from aqt.operations.scheduling import (
    empty_filtered_deck,
    rebuild_filtered_deck,
    unbury_deck,
)
from aqt.sound import av_player
from aqt.toolbar import BottomBar
from aqt.utils import askUserDialog, openLink, shortcut, tooltip, tr


class OverviewBottomBar:
    def __init__(self, overview: Overview) -> None:
        self.overview = overview


@dataclass
class OverviewContent:
    """Stores sections of HTML content that the overview will be
    populated with.

    Attributes:
        deck {str} -- Plain text deck name
        shareLink {str} -- HTML of the share link section
        desc {str} -- HTML of the deck description section
        table {str} -- HTML of the deck stats table section
    """

    deck: str
    shareLink: str
    desc: str
    table: str


class Overview:
    "Deck overview."

    def __init__(self, mw: aqt.AnkiQt) -> None:
        self.mw = mw
        self.web = mw.web
        self.bottom = BottomBar(mw, mw.bottomWeb)
        self._refresh_needed = False
        self._rwkv_count_generation = 0
        self._rwkv_counts_pending = False
        self._rwkv_retry_scheduled = False

    def show(self) -> None:
        av_player.stop_and_clear_queue()
        self.web.set_bridge_command(self._linkHandler, self)
        self.mw.setStateShortcuts(self._shortcutKeys())
        self.refresh()

    def refresh(self) -> None:
        self._rwkv_count_generation += 1
        generation = self._rwkv_count_generation

        def success(rwkv_counts_pending: bool) -> None:
            if generation != self._rwkv_count_generation:
                return
            self._refresh_needed = False
            self._rwkv_counts_pending = rwkv_counts_pending
            self._renderPage()
            self._renderBottom()
            self.mw.web.setFocus()
            gui_hooks.overview_did_refresh(self)
            if rwkv_counts_pending:
                self._retry_rwkv_counts()

        def get_counts(col: Collection) -> bool:
            rwkv_counts_pending = aqt.rwkv_scheduler.rwkv_state_cache_loading(self.mw)
            aqt.rwkv_scheduler.prepare_current_deck_review_queue_scores(
                self.mw,
                reason="overview counts",
            )
            col.sched.counts()
            pending = (
                rwkv_counts_pending
                or aqt.rwkv_scheduler.rwkv_state_cache_loading(self.mw)
                or aqt.rwkv_scheduler.rwkv_review_scores_pending(col)
            )
            return pending

        QueryOp(parent=self.mw, op=get_counts, success=success).run_in_background()

    def refresh_if_needed(self) -> None:
        if self._refresh_needed:
            self.refresh()

    def redraw_for_ui_mode(self) -> None:
        """Redraw after a Simple/Advanced switch without recomputing the due
        counts (spec ui.mode-switch): the New/Learn/Due counts table (spec
        ui.simple-mode-deck-counts) and the bottom bar's Custom Study button
        (spec ui.simple-mode-tools-hidden) both depend on the mode, so both
        are redrawn from scheduler state already loaded, the same cheap way
        the deck list's bottom row redraws in place."""
        self._renderPage()
        self._renderBottom()

    def _retry_rwkv_counts(self) -> None:
        """RWKV-Instant has not scored the deck yet: ask again in 2 s, unless
        RWKV cannot run (spec sched.rwkv-instant-waits)."""
        if self._rwkv_retry_scheduled or not aqt.rwkv_scheduler.rwkv_model_available():
            return
        self._rwkv_retry_scheduled = True

        def retry() -> None:
            self._rwkv_retry_scheduled = False
            if self.mw.state == "overview" and self._rwkv_counts_pending:
                self.refresh()

        self.mw.progress.single_shot(2000, retry, False)

    def op_executed(
        self, changes: OpChanges, handler: object | None, focused: bool
    ) -> bool:
        if changes.study_queues:
            self._refresh_needed = True

        if focused:
            self.refresh_if_needed()

        return self._refresh_needed

    # Handlers
    ############################################################

    def _linkHandler(self, url: str) -> bool:
        if url == "study":
            self.mw.col.startTimebox()
            self.mw.moveToState("review")
            if self.mw.state == "overview":
                tooltip(tr.studying_no_cards_are_due_yet())
        elif url == "anki":
            print("anki menu")
        elif url == "opts":
            display_options_for_deck(self.mw.col.decks.current())
        elif url == "cram":
            aqt.dialogs.open("FilteredDeckConfigDialog", self.mw)
        elif url == "refresh":
            self.rebuild_current_filtered_deck()
        elif url == "empty":
            self.empty_current_filtered_deck()
        elif url == "decks":
            self.mw.moveToState("deckBrowser")
        elif url == "review":
            openLink(f"{aqt.appShared}info/{self.sid}?v={self.sidVer}")
        elif url in {"studymore", "customStudy"}:
            self.onStudyMore()
        elif url == "unbury":
            self.on_unbury()
        elif url == "description":
            self.edit_description()
        elif url.lower().startswith("http"):
            openLink(url)
        return False

    def _shortcutKeys(self) -> list[tuple[str, Callable]]:
        return [
            ("o", lambda: display_options_for_deck(self.mw.col.decks.current())),
            ("r", self.rebuild_current_filtered_deck),
            ("e", self.empty_current_filtered_deck),
            ("c", self.onCustomStudyKey),
            ("u", self.on_unbury),
        ]

    def _current_deck_is_filtered(self) -> int:
        return self.mw.col.decks.current()["dyn"]

    def rebuild_current_filtered_deck(self) -> None:
        rebuild_filtered_deck(
            parent=self.mw, deck_id=self.mw.col.decks.selected()
        ).run_in_background()

    def empty_current_filtered_deck(self) -> None:
        empty_filtered_deck(
            parent=self.mw, deck_id=self.mw.col.decks.selected()
        ).run_in_background()

    def onCustomStudyKey(self) -> None:
        if not self._current_deck_is_filtered():
            self.onStudyMore()

    def on_unbury(self) -> None:
        mode = UnburyDeck.Mode.ALL
        info = self.mw.col.sched.congratulations_info()
        if info.have_sched_buried and info.have_user_buried:
            opts = [
                tr.studying_manually_buried_cards(),
                tr.studying_buried_siblings(),
                tr.studying_all_buried_cards(),
                tr.actions_cancel(),
            ]

            diag = askUserDialog(tr.studying_what_would_you_like_to_unbury(), opts)
            diag.setDefault(0)
            ret = diag.run()
            if ret == opts[0]:
                mode = UnburyDeck.Mode.USER_ONLY
            elif ret == opts[1]:
                mode = UnburyDeck.Mode.SCHED_ONLY
            elif ret == opts[3]:
                return

        unbury_deck(
            parent=self.mw, deck_id=self.mw.col.decks.get_current_id(), mode=mode
        ).run_in_background()

    onUnbury = on_unbury

    # HTML
    ############################################################

    def _renderPage(self) -> None:
        deck = self.mw.col.decks.current()
        self.sid = deck.get("sharedFrom")
        if self.sid:
            self.sidVer = deck.get("ver", None)
            shareLink = '<a class=smallLink href="review">Reviews and Updates</a>'
        else:
            shareLink = ""
        # no congratulations while RWKV-Instant's reviews are still unknown
        if not self._rwkv_counts_pending and self.mw.col.sched._is_finished():
            self._show_finished_screen()
            return
        content = OverviewContent(
            deck=deck["name"],
            shareLink=shareLink,
            desc=self._desc(deck),
            table=self._table() + self._rwkv_pending_notice(),
        )
        gui_hooks.overview_will_render_content(self, content)
        content.deck = html.escape(content.deck)
        self.web.stdHtml(
            self._body % content.__dict__,
            css=["css/overview.css"],
            js=["js/vendor/jquery.min.js"],
            context=self,
        )

    def _rwkv_pending_notice(self) -> str:
        if not self._rwkv_counts_pending:
            return ""
        notice = (
            tr.qt_misc_rwkv_instant_scores_pending()
            if aqt.rwkv_scheduler.rwkv_model_available()
            else tr.qt_misc_rwkv_model_not_found()
        )
        return f'<p class="rwkv-pending">{html.escape(notice)}</p>'

    def _show_finished_screen(self) -> None:
        self.web.load_sveltekit_page("congrats")

    def _desc(self, deck: dict[str, Any]) -> str:
        if deck["dyn"]:
            desc = tr.studying_this_is_a_special_deck_for()
            desc += f" {tr.studying_cards_will_be_automatically_returned_to()}"
            desc += f" {tr.studying_deleting_this_deck_from_the_deck()}"
        else:
            desc = deck.get("desc", "")
            if deck.get("md", False):
                desc = self.mw.col.render_markdown(desc)
        if not desc:
            return "<p>"
        if deck["dyn"]:
            dyn = "dyn"
        else:
            dyn = ""
        return f'<div class="descfont descmid description {dyn}">{desc}</div>'

    def _table(self) -> str:
        new_count, learning_count, review_count = self.mw.col.sched.counts()
        counts: list[int | str] = [new_count, learning_count, review_count]
        current_did = self.mw.col.decks.get_current_id()
        # the counts of the deck's node in deck_due_tree(), without the tree
        deck_counts = self.mw.col.sched.deck_due_counts(current_did)

        but = self.mw.button
        if self.mw.col.v3_scheduler():
            assert deck_counts is not None
            buried_new = deck_counts.new_count - new_count
            buried_learning = deck_counts.learn_count - learning_count
            if self._rwkv_counts_pending:
                counts[2] = "…"
                buried_review = 0
            else:
                buried_review = deck_counts.review_count - review_count
        else:
            buried_new = buried_learning = buried_review = 0
        buried_label = tr.studying_counts_differ()

        def number_row(
            title: str,
            klass: str,
            count: int | str,
            buried_count: int,
        ) -> str:
            buried = f"{buried_count:+}" if buried_count else ""
            return f"""
<tr>
    <td>{title}:</td>
    <td>
        <b>
            <span class={klass}>{count}</span>
            <span class=bury-count title="{buried_label}">{buried}</span>
        </b>
    </td>
</tr>
"""

        if aqt.ui_split.shown(self.mw, "main.learn_count"):
            rows = (
                number_row(tr.actions_new(), "new-count", counts[0], buried_new)
                + number_row(
                    tr.scheduling_learning(), "learn-count", counts[1], buried_learning
                )
                + number_row(
                    tr.studying_to_review(), "review-count", counts[2], buried_review
                )
            )
        else:
            # Simple mode shows one Due row that already includes Learn
            # (spec ui.simple-mode-deck-counts); only the display is
            # summed, self.mw.col.sched.counts() itself stays untouched.
            due_count = (
                counts[2] if isinstance(counts[2], str) else counts[2] + learning_count
            )
            rows = number_row(
                tr.actions_new(), "new-count", counts[0], buried_new
            ) + number_row(
                tr.studying_to_review(),
                "review-count",
                due_count,
                buried_review + buried_learning,
            )

        return f"""
<table width=400 cellpadding=5>
<tr><td align=center valign=top>
<table cellspacing=5>
{rows}
</table>
</td><td align=center>
{but("study", tr.studying_study_now(), id="study", extra=" autofocus")}</td></tr></table>"""

    _body = """
<center>
<h3>%(deck)s</h3>
%(shareLink)s
%(desc)s
%(table)s
</center>
"""

    def edit_description(self) -> None:
        DeckDescriptionDialog(self.mw)

    # Bottom area
    ######################################################################

    def _renderBottom(self) -> None:
        # the split decides which buttons show (spec
        # ui.simple-mode-tools-hidden, ui.split-configurable); a hidden
        # button keeps its keyboard shortcut
        shows = aqt.ui_split.visibility(self.mw)
        links = []
        if shows("main.overview.options"):
            links.append(["O", "opts", tr.actions_options()])
        is_dyn = self.mw.col.decks.current()["dyn"]
        if is_dyn:
            if shows("main.overview.rebuild"):
                links.append(["R", "refresh", tr.actions_rebuild()])
            if shows("main.overview.empty"):
                links.append(["E", "empty", tr.studying_empty()])
        elif shows("main.overview.custom_study"):
            links.append(["C", "studymore", tr.actions_custom_study()])
        # links.append(["F", "cram", _("Filter/Cram")])
        if self.mw.col.sched.have_buried() and shows("main.overview.unbury"):
            links.append(["U", "unbury", tr.studying_unbury()])
        if not is_dyn and shows("main.overview.description"):
            links.append(["", "description", tr.scheduling_description()])
        link_handler = gui_hooks.overview_will_render_bottom(
            self._linkHandler,
            links,
        )
        if not callable(link_handler):
            link_handler = self._linkHandler
        buf = ""
        for b in links:
            if b[0]:
                b[0] = tr.actions_shortcut_key(val=shortcut(b[0]))
            buf += """
<button title="%s" onclick='pycmd("%s")'>%s</button>""" % tuple(b)
        self.bottom.draw(
            buf=buf,
            link_handler=link_handler,
            web_context=OverviewBottomBar(self),
        )

    # Studying more
    ######################################################################

    def onStudyMore(self) -> None:
        import aqt.customstudy

        aqt.customstudy.CustomStudy.fetch_data_and_show(self.mw)
