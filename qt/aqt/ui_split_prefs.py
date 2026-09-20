# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The UI split tab of Preferences (spec ui.split-configurable): one
collapsible group per area, a "Show in Simple mode" checkbox per item, a
search field that filters the list and a "Reset to defaults" button.

Above the list sits the one setting of the tab that is not a checkbox: the
recall wording, with three choices (spec ui.simple-recall-wording).

A change is stored at once (in the collection config, aqt.ui_split) and the
open windows redraw in place, the same way a Simple | Advanced switch does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aqt import ui_split
from aqt.qt import (
    QComboBox,
    QFont,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    Qt,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.utils import askUser, tr

if TYPE_CHECKING:
    from aqt.main import AnkiQt

ITEM_ID_ROLE = Qt.ItemDataRole.UserRole
CHECK_COLUMN = 1


def matches(query: str, *texts: str) -> bool:
    """Whether a search matches: every word of it is in one of the texts."""
    haystack = " ".join(texts).casefold()
    return all(word in haystack for word in query.casefold().split())


class UiSplitPreferences(QWidget):
    def __init__(self, mw: AnkiQt) -> None:
        super().__init__()
        self.mw = mw
        layout = QVBoxLayout(self)

        explanation = QLabel(tr.preferences_ui_split_explanation())
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        layout.addLayout(self._recall_wording_row())

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr.actions_search())
        self.search.setClearButtonEnabled(True)
        qconnect(self.search.textChanged, self.apply_filter)
        row.addWidget(self.search, 1)
        self.reset_button = QPushButton(tr.preferences_ui_split_reset())
        self.reset_button.setAutoDefault(False)
        qconnect(self.reset_button.clicked, self.on_reset)
        row.addWidget(self.reset_button)
        layout.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(
            [tr.preferences_ui_split_item(), tr.preferences_ui_split_show_in_simple()]
        )
        header = self.tree.header()
        assert header is not None
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(
            CHECK_COLUMN, QHeaderView.ResizeMode.ResizeToContents
        )
        self.tree.setUniformRowHeights(True)
        layout.addWidget(self.tree, 1)

        self._leaves: list[QTreeWidgetItem] = []
        self._build()
        qconnect(self.tree.itemChanged, self._on_item_changed)

    # The recall wording (spec ui.simple-recall-wording)
    ######################################################################

    def _recall_wording_row(self) -> QHBoxLayout:
        """The one setting of this tab with three choices instead of a
        "Show in Simple mode" checkbox."""
        row = QHBoxLayout()
        label = QLabel(tr.preferences_recall_wording())
        label.setWordWrap(True)
        row.addWidget(label)
        self.recall_wording = QComboBox()
        for wording, text in (
            (ui_split.RECALL_BY_MODE, tr.preferences_recall_wording_by_mode()),
            (ui_split.RECALL_TECHNICAL, tr.preferences_recall_wording_technical()),
            (ui_split.RECALL_PLAIN, tr.preferences_recall_wording_plain()),
        ):
            self.recall_wording.addItem(text, wording)
        self._load_recall_wording()
        qconnect(self.recall_wording.currentIndexChanged, self._on_recall_wording)
        row.addWidget(self.recall_wording)
        row.addStretch()
        return row

    def _load_recall_wording(self) -> None:
        index = self.recall_wording.findData(ui_split.recall_wording(self.mw.col))
        self.recall_wording.blockSignals(True)
        self.recall_wording.setCurrentIndex(max(0, index))
        self.recall_wording.blockSignals(False)

    def _on_recall_wording(self, _index: int) -> None:
        ui_split.set_recall_wording(self.mw.col, self.recall_wording.currentData())
        self._relabel()
        self.mw.redraw_for_ui_split()

    def _relabel(self) -> None:
        """The item names that hold the word (the Browser's Retrievability
        column, the Stats graph) follow the setting at once."""
        for leaf in self._leaves:
            leaf.setText(0, ui_split.ITEMS_BY_ID[leaf.data(0, ITEM_ID_ROLE)].label())

    # Building
    ######################################################################

    def _build(self) -> None:
        bold = QFont()
        bold.setBold(True)
        areas: dict[ui_split.Area, QTreeWidgetItem] = {}
        groups: dict[tuple[ui_split.Area, str], QTreeWidgetItem] = {}
        for item in ui_split.ITEMS:
            parent = areas.get(item.area)
            if parent is None:
                parent = QTreeWidgetItem(self.tree, [item.area_label()])
                parent.setFont(0, bold)
                parent.setFirstColumnSpanned(True)
                areas[item.area] = parent
            if item.group is not None:
                name = item.group()
                key = (item.area, name)
                group = groups.get(key)
                if group is None:
                    group = QTreeWidgetItem(parent, [name])
                    group.setFirstColumnSpanned(True)
                    groups[key] = group
                parent = group
            leaf = QTreeWidgetItem(parent, [item.label()])
            leaf.setData(0, ITEM_ID_ROLE, item.id)
            leaf.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            self._leaves.append(leaf)
        for group in groups.values():
            group.setExpanded(True)
        self._load_checks()

    def _load_checks(self) -> None:
        choices = ui_split.overrides(self.mw.col)
        self.tree.blockSignals(True)
        for leaf in self._leaves:
            item_id = leaf.data(0, ITEM_ID_ROLE)
            shown = ui_split.shown_in_simple(None, item_id, choices)
            leaf.setCheckState(
                CHECK_COLUMN,
                Qt.CheckState.Checked if shown else Qt.CheckState.Unchecked,
            )
        self.tree.blockSignals(False)

    # Editing
    ######################################################################

    def _on_item_changed(self, leaf: QTreeWidgetItem, column: int) -> None:
        item_id = leaf.data(0, ITEM_ID_ROLE)
        if column != CHECK_COLUMN or item_id is None:
            return
        shown = leaf.checkState(CHECK_COLUMN) == Qt.CheckState.Checked
        ui_split.set_shown_in_simple(self.mw.col, item_id, shown)
        self.mw.redraw_for_ui_split()

    def on_reset(self) -> None:
        if not askUser(tr.preferences_ui_split_reset_confirm(), parent=self):
            return
        ui_split.reset(self.mw.col)
        self._load_checks()
        self._load_recall_wording()
        self._relabel()
        self.mw.redraw_for_ui_split()

    # Search
    ######################################################################

    def apply_filter(self, query: str) -> None:
        query = query.strip()
        root = self.tree.invisibleRootItem()
        assert root is not None
        for index in range(root.childCount()):
            area = root.child(index)
            assert area is not None
            self._filter(area, query, ())
            if query:
                area.setExpanded(True)

    def _filter(
        self, node: QTreeWidgetItem, query: str, context: tuple[str, ...]
    ) -> bool:
        """Show the leaves that match and the groups that hold one; return
        whether anything under ``node`` shows."""
        texts = (*context, node.text(0))
        if node.childCount() == 0:
            visible = not query or matches(query, *texts)
        else:
            visible = False
            for index in range(node.childCount()):
                child = node.child(index)
                assert child is not None
                visible = self._filter(child, query, texts) or visible
            if query and visible:
                node.setExpanded(True)
        node.setHidden(not visible)
        return visible
