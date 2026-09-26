# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from types import SimpleNamespace

import pytest

from anki.decks import DeckTreeNode


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch):
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        tr,
        "decks_review_limit_tooltip",
        lambda *, total, count: f'{total} due; limit allows {count} "reviews".',
    )
    from aqt.deckbrowser import DeckBrowser

    browser = DeckBrowser.__new__(DeckBrowser)
    browser._rwkv_pending_deck_ids = set()
    browser._pending_collapse = {}
    browser._rwkv_count_generation = 0
    browser.mw = SimpleNamespace(advanced_ui=lambda: True)
    return browser


def test_review_limit_totals_include_collapsed_children(browser):
    child = DeckTreeNode(deck_id=2, review_count=40, review_uncapped=100, level=2)
    parent = DeckTreeNode(
        deck_id=1,
        review_count=65,
        review_uncapped=42,
        children=[child],
        collapsed=True,
        level=1,
    )
    tree = DeckTreeNode(children=[parent])
    browser._render_data = SimpleNamespace(tree=tree, current_deck_id=1)

    labels = browser._review_limit_labels(tree)
    assert labels[1] == (" (/142)", '142 due; limit allows 65 "reviews".')
    assert labels[2][0] == " (/100)"
    rendered = browser._renderDeckTree(tree)
    assert 'class="review-count">65</span>' in rendered
    assert (
        'title="142 due; limit allows 65 &quot;reviews&quot;."> (/142)</span>'
        in rendered
    )
    assert 'id="deck-2-review-limit"' not in rendered


@pytest.mark.parametrize(
    "count,total,expected", [(0, 10, " (/10)"), (10, 10, ""), (0, 0, "")]
)
def test_review_limit_only_shown_when_count_reduced(browser, count, total, expected):
    tree = DeckTreeNode(
        children=[DeckTreeNode(deck_id=1, review_count=count, review_uncapped=total)]
    )
    assert browser._review_limit_labels(tree)[1][0] == expected


def test_review_limit_refresh_clears_pending_and_unlimited_labels(browser):
    import json

    node = DeckTreeNode(deck_id=1, review_count=5, review_uncapped=10)
    tree = DeckTreeNode(children=[node])
    scripts = []
    browser.web = SimpleNamespace(eval=scripts.append)
    browser._render_data = SimpleNamespace(tree=tree)

    def refresh_labels():
        browser._render_rwkv_deck_counts()
        encoded = scripts[-1].split("const limitLabels = ", 1)[1].split(";\n", 1)[0]
        return json.loads(encoded)["1"]

    browser._rwkv_pending_deck_ids = {1}
    assert refresh_labels() == ["", ""]
    browser._rwkv_pending_deck_ids.clear()
    assert refresh_labels()[0] == " (/10)"
    tree.children[0].review_count = 10
    assert refresh_labels() == ["", ""]


def _collapsible(browser, monkeypatch):
    """A deck browser whose page was drawn once, with the collapse op and
    the page reload replaced by recorders."""
    from aqt import deckbrowser

    child = DeckTreeNode(deck_id=2, name="child", level=2)
    parent = DeckTreeNode(deck_id=1, name="parent", children=[child], level=1)
    tree = DeckTreeNode(children=[parent])
    browser._render_data = SimpleNamespace(
        tree=tree, current_deck_id=1, studied_today="studied"
    )
    browser._rendered_stats = browser._renderStats()
    scripts: list[str] = []
    reloads: list[bool] = []
    browser.web = SimpleNamespace(eval=scripts.append)
    browser._renderPage = lambda reuse=False: reloads.append(reuse)
    browser.mw = SimpleNamespace(
        advanced_ui=lambda: True,
        col=SimpleNamespace(
            decks=SimpleNamespace(
                find_deck_in_tree=lambda tree, did: parent if did == 1 else None
            )
        ),
    )
    ops: list[bool] = []
    monkeypatch.setattr(
        deckbrowser,
        "set_deck_collapsed",
        lambda **kwargs: SimpleNamespace(
            run_in_background=lambda initiator: ops.append(kwargs["collapsed"])
        ),
    )
    return parent, scripts, reloads, ops


def test_collapse_swaps_the_deck_table_without_reloading(browser, monkeypatch):
    from aqt import gui_hooks

    monkeypatch.setattr(gui_hooks.webview_will_set_content, "_hooks", [])
    monkeypatch.setattr(gui_hooks.deck_browser_did_render, "_hooks", [])
    monkeypatch.setattr(gui_hooks.deck_browser_will_render_content, "_hooks", [])
    parent, scripts, reloads, ops = _collapsible(browser, monkeypatch)

    browser._collapse(1)

    assert parent.collapsed and ops == [True] and reloads == []
    assert len(scripts) == 1 and scripts[0].startswith("replaceDeckTree(")
    # the swapped rows are the rows a full draw would put in the table
    assert browser._renderDeckTree(browser._render_data.tree) in json_arg(scripts[0])


def json_arg(script: str) -> str:
    import json

    return json.loads(script[len("replaceDeckTree(") : -len(");")])


def addon_handler(*args):
    pass


addon_handler.__module__ = "1234567890.addon"


@pytest.mark.parametrize(
    "hook_name",
    ["webview_will_set_content", "deck_browser_did_render"],
)
def test_collapse_reloads_when_an_addon_decorates_the_page(
    browser, monkeypatch, hook_name
):
    from aqt import gui_hooks

    for name in ("webview_will_set_content", "deck_browser_did_render"):
        handlers = [addon_handler] if name == hook_name else []
        monkeypatch.setattr(getattr(gui_hooks, name), "_hooks", handlers)
    monkeypatch.setattr(gui_hooks.deck_browser_will_render_content, "_hooks", [])
    _, scripts, reloads, _ = _collapsible(browser, monkeypatch)

    browser._collapse(1)

    assert scripts == [] and reloads == [True]


def test_collapse_reloads_when_the_stats_section_would_change(browser, monkeypatch):
    from aqt import gui_hooks

    monkeypatch.setattr(gui_hooks.webview_will_set_content, "_hooks", [])
    monkeypatch.setattr(gui_hooks.deck_browser_did_render, "_hooks", [])

    def add_stats(deck_browser, content):
        content.stats += "<div>new</div>"

    monkeypatch.setattr(
        gui_hooks.deck_browser_will_render_content, "_hooks", [add_stats]
    )
    _, scripts, reloads, _ = _collapsible(browser, monkeypatch)

    browser._collapse(1)

    assert scripts == [] and reloads == [True]


def test_collapse_reloads_when_the_table_gets_a_script(browser, monkeypatch):
    from aqt import gui_hooks

    monkeypatch.setattr(gui_hooks.webview_will_set_content, "_hooks", [])
    monkeypatch.setattr(gui_hooks.deck_browser_did_render, "_hooks", [])

    def add_script(deck_browser, content):
        content.tree += "<SCRIPT>decorate()</script>"

    monkeypatch.setattr(
        gui_hooks.deck_browser_will_render_content, "_hooks", [add_script]
    )
    _, scripts, reloads, _ = _collapsible(browser, monkeypatch)

    browser._collapse(1)

    assert scripts == [] and reloads == [True]


# Pins spec/ui.md#ui.get-decks-and-get-addons.
def test_deck_list_shows_get_addons_in_advanced_mode_only(browser):
    browser.mw = SimpleNamespace(advanced_ui=lambda: False)
    simple = browser._buttons_html()
    assert 'pycmd("get_addons")' not in simple
    assert 'pycmd("shared")' in simple

    browser.mw = SimpleNamespace(advanced_ui=lambda: True)
    advanced = browser._buttons_html()
    assert 'pycmd("get_addons")' in advanced


@pytest.mark.parametrize(
    "text,label",
    [
        ("Get Add-ons...", "Get Add-ons"),
        ("Add-ons holen …", "Add-ons holen"),
        ("Get Add-ons", "Get Add-ons"),
    ],
)
def test_deck_list_get_addons_label_has_no_trailing_dots(
    browser, monkeypatch, text, label
):
    """The deck list's Get Add-ons button carries no "..."; the Add-ons
    dialog's button, which shares the string, keeps it."""
    from aqt import deckbrowser
    from aqt.utils import tr

    monkeypatch.setattr(tr, "addons_get_addons", lambda: text)
    assert deckbrowser._get_addons_label() == label


def test_get_addons_button_opens_the_existing_install_dialog(browser, monkeypatch):
    """The deck list's Get Add-ons button opens the same GetAddons dialog as
    Tools > Add-ons > Get Add-ons, unchanged (spec
    ui.get-decks-and-get-addons)."""
    import aqt.addons as addons_module

    calls: list[tuple] = []

    class FakeGetAddons:
        def __init__(self, parent: object, mgr: object) -> None:
            calls.append((parent, mgr))
            self.ids = [123]

    downloaded: list[tuple] = []
    monkeypatch.setattr(addons_module, "GetAddons", FakeGetAddons)
    monkeypatch.setattr(
        addons_module,
        "download_addons",
        lambda parent, mgr, ids, on_done, force_enable=False: downloaded.append(
            (parent, mgr, ids, force_enable)
        ),
    )
    mw = SimpleNamespace(addonManager="mgr-stub")
    browser.mw = mw

    browser._on_get_addons()

    assert calls == [(mw, "mgr-stub")]
    assert downloaded == [(mw, "mgr-stub", [123], True)]


def test_get_addons_button_does_nothing_when_no_code_was_entered(browser, monkeypatch):
    import aqt.addons as addons_module

    class FakeGetAddons:
        def __init__(self, parent: object, mgr: object) -> None:
            self.ids = []

    downloaded: list[tuple] = []
    monkeypatch.setattr(addons_module, "GetAddons", FakeGetAddons)
    monkeypatch.setattr(
        addons_module,
        "download_addons",
        lambda *args, **kwargs: downloaded.append((args, kwargs)),
    )
    browser.mw = SimpleNamespace(addonManager="mgr-stub")

    browser._on_get_addons()

    assert downloaded == []


# Pins spec/ui.md#ui.simple-mode-deck-counts.
def test_deck_list_header_hides_learn_column_in_simple_mode(browser, monkeypatch):
    from aqt.utils import tr

    monkeypatch.setattr(tr, "decks_learn_header", lambda: "LEARN_HEADER_MARKER")
    browser.mw = SimpleNamespace(advanced_ui=lambda: False)
    tree = DeckTreeNode(children=[])
    browser._render_data = SimpleNamespace(tree=tree, current_deck_id=1)

    rendered = browser._renderDeckTree(tree)

    assert "LEARN_HEADER_MARKER" not in rendered


def test_deck_list_header_shows_learn_column_in_advanced_mode(browser, monkeypatch):
    from aqt.utils import tr

    monkeypatch.setattr(tr, "decks_learn_header", lambda: "LEARN_HEADER_MARKER")
    browser.mw = SimpleNamespace(advanced_ui=lambda: True)
    tree = DeckTreeNode(children=[])
    browser._render_data = SimpleNamespace(tree=tree, current_deck_id=1)

    rendered = browser._renderDeckTree(tree)

    assert "LEARN_HEADER_MARKER" in rendered


def test_deck_row_sums_learn_into_due_in_simple_mode(browser):
    browser.mw = SimpleNamespace(advanced_ui=lambda: False)
    node = DeckTreeNode(deck_id=1, new_count=3, learn_count=4, review_count=5)
    tree = DeckTreeNode(children=[node])
    browser._render_data = SimpleNamespace(tree=tree, current_deck_id=1)

    rendered = browser._renderDeckTree(tree)

    assert 'id="deck-1-learn-count"' not in rendered
    assert 'id="deck-1-review-count" class="review-count">9</span>' in rendered


def test_deck_row_keeps_learn_and_due_separate_in_advanced_mode(browser):
    browser.mw = SimpleNamespace(advanced_ui=lambda: True)
    node = DeckTreeNode(deck_id=1, new_count=3, learn_count=4, review_count=5)
    tree = DeckTreeNode(children=[node])
    browser._render_data = SimpleNamespace(tree=tree, current_deck_id=1)

    rendered = browser._renderDeckTree(tree)

    assert 'id="deck-1-learn-count" class="learn-count">4</span>' in rendered
    assert 'id="deck-1-review-count" class="review-count">5</span>' in rendered


def test_rwkv_deck_count_update_sums_learn_into_due_in_simple_mode(browser):
    import json

    browser.mw = SimpleNamespace(advanced_ui=lambda: False)
    node = DeckTreeNode(deck_id=1, new_count=3, learn_count=4, review_count=5)
    tree = DeckTreeNode(children=[node])
    browser._render_data = SimpleNamespace(tree=tree)
    scripts: list[str] = []
    browser.web = SimpleNamespace(eval=scripts.append)

    browser._render_rwkv_deck_counts()

    rows = json.loads(scripts[-1].split("const rows = ", 1)[1].split(";\n", 1)[0])
    assert rows == [[1, 3, 4, 9]]


# Pins spec/ui.md#ui.mode-switch: the RWKV submenu of a deck's gear menu names
# the scope of the reschedule, not the algorithm (the submenu is called RWKV).
def test_the_rwkv_submenu_entries_name_the_scope():
    from pathlib import Path

    ftl = Path(__file__).parents[2] / "ftl" / "core" / "decks.ftl"
    lines = ftl.read_text(encoding="utf-8").splitlines()

    assert "decks-reschedule-with-rwkv-curve = Reschedule this deck" in lines
    assert "decks-rwkv-reschedule-all-decks = Reschedule all decks" in lines


class _FakeQueryOp:
    """Runs the deck list's background step when the test asks for it."""

    def __init__(self, *, parent, op, success):
        self.op = op
        self.success = success
        self.parent = parent

    def run_in_background(self, initiator=None):
        return self


class _Page:
    """The deck list drawn into a fake webview, with the calls it made."""

    def __init__(self):
        self.html: list[str] = []
        self.scripts: list[str] = []
        self.offset_requests: list = []
        self.buttons = 0
        self.held: list[bool] = []

    def stdHtml(self, html, css=None, js=None, context=None, held=False):
        self.html.append(html)
        self.held.append(held)

    def eval(self, script):
        self.scripts.append(script)

    def evalWithCallback(self, script, callback):
        assert script == "window.pageYOffset"
        self.offset_requests.append(callback)

    def adjustHeightToFit(self):
        pass

    def set_bridge_command(self, handler, context):
        pass

    def scrolls(self) -> list[str]:
        return [s for s in self.scripts if s.startswith("window.scrollTo(")]

    def swaps(self) -> list[str]:
        return [s for s in self.scripts if s.startswith("replaceDeckTree(")]


def _tree(*, collapsed=False, new=1, review=2, child_review=3):
    child = DeckTreeNode(
        deck_id=2, name="child", level=2, new_count=new, review_count=child_review
    )
    parent = DeckTreeNode(
        deck_id=1,
        name="parent",
        level=1,
        children=[child],
        collapsed=collapsed,
        new_count=new,
        review_count=review,
    )
    return DeckTreeNode(children=[parent])


@pytest.fixture
def refreshable(browser, monkeypatch):
    """A deck browser whose page was drawn once into a fake webview, with the
    background step of the next draw under the test's control."""
    from aqt import deckbrowser, gui_hooks, review_heatmap, rwkv_scheduler

    for hook in (
        gui_hooks.webview_will_set_content,
        gui_hooks.deck_browser_did_render,
        gui_hooks.deck_browser_will_render_content,
    ):
        monkeypatch.setattr(hook, "_hooks", [])
    monkeypatch.setattr(review_heatmap, "instance", lambda: None)
    monkeypatch.setattr(
        rwkv_scheduler, "clear_deck_browser_rwkv_count_scores", lambda mw: None
    )
    monkeypatch.setattr(
        rwkv_scheduler, "deck_browser_rwkv_count_scope_ids", lambda mw, tree: ()
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "prepare_deck_browser_rwkv_counts_incrementally",
        lambda *args, **kwargs: None,
    )
    ops: list = []
    monkeypatch.setattr(
        deckbrowser,
        "QueryOp",
        lambda **kwargs: ops.append(_FakeQueryOp(**kwargs)) or ops[-1],
    )
    monkeypatch.setattr(
        deckbrowser,
        "set_deck_collapsed",
        lambda **kwargs: SimpleNamespace(run_in_background=lambda initiator: None),
    )

    page = _Page()
    state = SimpleNamespace(tree=_tree(), studied_today="studied today")
    browser.web = page
    browser.bottom = SimpleNamespace(web=SimpleNamespace())
    reveal = SimpleNamespace(expect=lambda web: None)
    monkeypatch.setattr(deckbrowser, "page_reveal", lambda: reveal)
    browser.mw = SimpleNamespace(
        state="deckBrowser",
        advanced_ui=lambda: True,
        toolbar=SimpleNamespace(redraw=lambda: None),
        col=SimpleNamespace(
            decks=SimpleNamespace(
                find_deck_in_tree=lambda tree, did: (
                    tree.children[0] if did == 1 else None
                )
            )
        ),
    )
    browser._drawButtons = lambda: setattr(page, "buttons", page.buttons + 1)

    def deliver():
        """Run the background step of the draw that is waiting and hand its
        data to the main thread, as QueryOp does."""
        op = ops.pop(0)
        col = SimpleNamespace(
            sched=SimpleNamespace(deck_due_tree=lambda: state.tree),
            decks=SimpleNamespace(get_current_id=lambda: 1),
            studied_today=lambda: state.studied_today,
            v3_scheduler=lambda: True,
        )
        op.success(op.op(col))

    monkeypatch.setattr(deckbrowser.av_player, "stop_and_clear_queue", lambda: None)
    browser.show()
    deliver()
    page.html.clear()
    page.scripts.clear()
    return SimpleNamespace(browser=browser, page=page, state=state, deliver=deliver)


def test_the_deck_list_is_drawn_hidden_and_shown_with_its_bottom_bar(refreshable):
    # spec ui.screen-one-frame
    refreshable.page.held.clear()
    refreshable.browser.show()
    refreshable.deliver()

    assert refreshable.page.held == [True]


def test_show_draws_the_deck_list_at_the_top(refreshable):
    page = refreshable.page
    # the first draw, made by the fixture, asked for no scroll position
    assert page.offset_requests == []
    assert refreshable.browser._page_is_drawn()

    refreshable.browser.show()
    refreshable.deliver()

    assert len(page.html) == 1 and page.offset_requests == [] and page.scrolls() == []


def test_refresh_swaps_the_deck_table_in_place_when_the_page_can_stay(refreshable):
    browser, page = refreshable.browser, refreshable.page
    refreshable.state.tree = _tree(new=7, review=9)

    browser.refresh()
    refreshable.deliver()

    # the page stayed, so nothing could move it
    assert page.html == [] and page.offset_requests == [] and page.scrolls() == []
    assert len(page.swaps()) == 1
    swapped = json_arg(page.swaps()[0])
    assert 'class="new-count">7</span>' in swapped
    assert 'class="review-count">9</span>' in swapped


def test_refresh_keeps_the_scroll_position_of_the_open_page(refreshable):
    browser, page = refreshable.browser, refreshable.page
    # a new heatmap and a new "studied today" make the stats section differ,
    # so the page has to be drawn again
    refreshable.state.studied_today = "studied more today"
    refreshable.state.tree = _tree(new=7)

    browser.refresh()
    refreshable.deliver()

    assert page.html == [] and len(page.offset_requests) == 1
    page.offset_requests[0](180)

    assert len(page.html) == 1
    assert "studied more today" in page.html[0]
    assert 'class="new-count">7</span>' in page.html[0]
    assert page.scrolls() == ["window.scrollTo(0, 180, 'instant');"]


def test_refresh_draws_from_the_top_on_another_screen(refreshable):
    browser, page = refreshable.browser, refreshable.page
    # an add-on may call refresh() while another screen holds the webview
    browser.mw.state = "review"

    browser.refresh()
    refreshable.deliver()

    assert len(page.html) == 1
    assert page.offset_requests == [] and page.scrolls() == [] and page.swaps() == []


def test_a_collapse_during_a_refresh_survives_the_refresh(refreshable):
    browser, page = refreshable.browser, refreshable.page

    browser.refresh()
    # the user collapses the deck while the counts are read
    browser._collapse(1)
    assert browser._pending_collapse == {1: True}
    # the data was read before the collapse was written
    refreshable.state.tree = _tree(collapsed=False, new=7)
    refreshable.deliver()

    assert browser._render_data.tree.children[0].collapsed
    assert browser._pending_collapse == {1: True}
    swapped = json_arg(page.swaps()[-1])
    assert 'class="new-count">7</span>' in swapped
    assert "child" not in swapped

    # once the written state comes back, the deck browser stops correcting it
    browser.refresh()
    refreshable.state.tree = _tree(collapsed=True)
    refreshable.deliver()
    assert browser._pending_collapse == {}
