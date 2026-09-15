# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any, cast

import pytest

import aqt.browser.table.table as table_module
from aqt.browser.table import CellRow
from aqt.browser.table.model import DataModel
from aqt.browser.table.table import Table
from aqt.qt import QAbstractTableModel, QItemSelection, QItemSelectionRange


def make_model(rows: int, columns: int, disabled: list[int]) -> Any:
    """A browser table model with `rows` card ids, some of them deleted
    (disabled) and some cached as normal rows."""
    model = cast(Any, DataModel.__new__(DataModel))
    QAbstractTableModel.__init__(model)
    model._state = SimpleNamespace(active_columns=[f"c{i}" for i in range(columns)])
    model._items = list(range(1000, 1000 + rows))
    model._rows = {}
    model._block_updates = False
    for row in range(0, rows, 3):
        model._rows[model._items[row]] = CellRow.generic(columns, "text")
    for row in disabled:
        model._rows[model._items[row]] = CellRow.disabled(columns, "deleted")
    return model


def random_selection(rng: random.Random, model: Any) -> QItemSelection:
    rows, columns = model.rowCount(), model.columnCount()
    selection = QItemSelection()
    for _ in range(rng.randint(0, 6)):
        top = rng.randrange(rows)
        bottom = rng.randrange(top, rows)
        left = rng.randrange(columns)
        right = rng.randrange(left, columns)
        # append, not select, so ranges may overlap and cells may repeat
        selection.append(
            QItemSelectionRange(model.index(top, left), model.index(bottom, right))
        )
    return selection


@pytest.mark.parametrize("seed", range(40))
def test_enabled_cells_are_counted_as_qt_counts_them(seed: int) -> None:
    rng = random.Random(seed)
    rows, columns = rng.randint(1, 80), rng.randint(1, 7)
    disabled = rng.sample(range(rows), rng.randint(0, rows // 3))
    model = make_model(rows, columns, disabled)
    selection = random_selection(rng, model)
    assert model.count_enabled_cells(selection) == len(selection.indexes())


def test_whole_table_and_empty_selection_are_counted_as_qt_counts_them() -> None:
    for disabled in ([], [0, 5, 49]):
        model = make_model(50, 6, disabled)
        everything = QItemSelection(model.index(0, 0), model.index(49, 5))
        assert model.count_enabled_cells(everything) == len(everything.indexes())
        assert model.count_enabled_cells(QItemSelection()) == 0


class ModifiersPressed:
    shift = False
    control = True


def test_ctrl_selection_change_counts_rows(monkeypatch: Any) -> None:
    monkeypatch.setattr(table_module, "KeyboardModifiersPressed", ModifiersPressed)
    model = make_model(50, 6, [7, 8])
    table = cast(Any, Table.__new__(Table))
    table._model = model
    table._len_selection = 1
    table._selected_rows = None
    table.browser = SimpleNamespace(on_all_or_selected_rows_changed=lambda: None)

    # Ctrl+A with only row 3 selected before: 49 rows are added, and the
    # deleted rows 7 and 8 do not count
    selected = QItemSelection(model.index(0, 0), model.index(2, 5))
    selected.merge(
        QItemSelection(model.index(4, 0), model.index(49, 5)),
        table_module.QItemSelectionModel.SelectionFlag.Select,
    )
    Table._on_selection_changed(table, selected, QItemSelection())
    assert table._len_selection == 48

    # Ctrl+click on row 3 again removes it
    deselected = QItemSelection(model.index(3, 0), model.index(3, 5))
    Table._on_selection_changed(table, QItemSelection(), deselected)
    assert table._len_selection == 47
