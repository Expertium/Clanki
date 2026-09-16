# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from aqt.browser.sidebar.item import SidebarItem, SidebarItemType
from aqt.browser.sidebar.model import SidebarModel
from aqt.browser.sidebar.tree import SidebarTreeView
from aqt.qt import QAbstractItemModel, QModelIndex, Qt

ENABLED = Qt.ItemFlag.ItemIsEnabled
SELECTABLE = Qt.ItemFlag.ItemIsSelectable
DRAGGABLE = Qt.ItemFlag.ItemIsDragEnabled
DROPPABLE = Qt.ItemFlag.ItemIsDropEnabled
EDITABLE = Qt.ItemFlag.ItemIsEditable


def make_item(
    name: str,
    item_type: SidebarItemType = SidebarItemType.CUSTOM,
    expanded: bool = False,
) -> SidebarItem:
    return SidebarItem(name, "", item_type=item_type, expanded=expanded)


def make_tree() -> SidebarItem:
    """A root with two sections, each with children and grandchildren."""
    root = make_item("", SidebarItemType.ROOT)
    decks = make_item("Decks", SidebarItemType.DECK_ROOT, expanded=True)
    root.add_child(decks)
    for name in ("Main", "Maths"):
        deck = make_item(name, SidebarItemType.DECK, expanded=name == "Main")
        decks.add_child(deck)
        deck.add_child(make_item(f"{name}::sub", SidebarItemType.DECK))
    tags = make_item("Tags", SidebarItemType.TAG_ROOT)
    root.add_child(tags)
    tags.add_child(make_item("marked", SidebarItemType.TAG, expanded=True))
    return root


def make_model(root: SidebarItem, valid_drop_types: tuple = ()) -> Any:
    model = cast(Any, SidebarModel.__new__(SidebarModel))
    QAbstractItemModel.__init__(model)
    model.sidebar = SimpleNamespace(valid_drop_types=valid_drop_types)
    model.root = root
    model._cache_rows(root)
    return model


class FakeView:
    """Enough of SidebarTreeView for _expand_where_necessary()."""

    def __init__(self) -> None:
        self.expanded: list[str] = []
        self.current: list[str] = []
        self.scrolled: list[str] = []

    def setExpanded(self, index: QModelIndex, expand: bool) -> None:
        assert expand is True
        self.expanded.append(index.internalPointer().name)

    def scrollTo(self, index: QModelIndex, _hint: Any) -> None:
        self.scrolled.append(index.internalPointer().name)

    def _selection_model(self) -> Any:
        view = self

        class Selection:
            def setCurrentIndex(self, index: QModelIndex, _flag: Any) -> None:
                view.current.append(index.internalPointer().name)

        return Selection()


def expand(model: Any, searching: bool) -> FakeView:
    view = FakeView()
    SidebarTreeView._expand_where_necessary(
        cast(Any, view), model, searching=searching
    )
    return view


def test_sidebar_flags_say_what_can_be_selected_dragged_dropped_and_renamed() -> None:
    root = make_tree()
    model = make_model(root, valid_drop_types=(SidebarItemType.DECK,))
    assert model.flags(QModelIndex()) == ENABLED

    def flags_of(name: str) -> Qt.ItemFlag:
        item = next(i for i in walk(root) if i.name == name)
        return model.flags(model.index_for_item(item))

    # every item can be selected and dragged
    assert flags_of("Decks") == ENABLED | SELECTABLE | DRAGGABLE
    # decks are a valid drop target here, and can be renamed
    assert flags_of("Main") == ENABLED | SELECTABLE | DRAGGABLE | DROPPABLE | EDITABLE
    # tags can be renamed, but are not a drop target in this sidebar
    assert flags_of("marked") == ENABLED | SELECTABLE | DRAGGABLE | EDITABLE
    # with no drop types, nothing is droppable
    other = make_model(root)
    item = next(i for i in walk(root) if i.name == "Main")
    assert other.flags(other.index_for_item(item)) == (
        ENABLED | SELECTABLE | DRAGGABLE | EDITABLE
    )


def walk(item: SidebarItem):
    for child in item.children:
        yield child
        yield from walk(child)


def test_sidebar_expands_the_items_that_ask_for_it_deepest_first() -> None:
    model = make_model(make_tree())
    view = expand(model, searching=False)
    # the expanded flag of each item decides; children are visited first
    assert view.expanded == ["Main", "Decks", "marked"]
    assert view.current == [] and view.scrolled == []


def test_sidebar_search_expands_matches_and_scrolls_to_the_first_one() -> None:
    root = make_tree()
    model = make_model(root)
    assert model.search("ma") is True
    view = expand(model, searching=True)
    # parents of a match open; a matching section root opens one level
    assert view.expanded == ["Main", "Maths", "Decks", "Tags"]
    # the first match in visit order (deepest child first) becomes current, once
    assert view.current == ["Main::sub"]
    assert view.scrolled == ["Main::sub"]


def test_sidebar_search_without_a_match_expands_nothing() -> None:
    model = make_model(make_tree())
    assert model.search("zzz") is False
    view = expand(model, searching=True)
    assert view.expanded == [] and view.current == []
