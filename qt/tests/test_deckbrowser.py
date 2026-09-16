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
        col=SimpleNamespace(
            decks=SimpleNamespace(
                find_deck_in_tree=lambda tree, did: parent if did == 1 else None
            )
        )
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


# Pins spec/ui.md#ui.mode-switch: the RWKV submenu of a deck's gear menu names
# the scope of the reschedule, not the algorithm (the submenu is called RWKV).
def test_the_rwkv_submenu_entries_name_the_scope():
    from pathlib import Path

    ftl = Path(__file__).parents[2] / "ftl" / "core" / "decks.ftl"
    lines = ftl.read_text(encoding="utf-8").splitlines()

    assert "decks-reschedule-with-rwkv-curve = Reschedule this deck" in lines
    assert "decks-rwkv-reschedule-all-decks = Reschedule all decks" in lines
