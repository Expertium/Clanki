# User interface modes

## ui.mode-switch

Given a collection, the interface is in Simple mode unless the collection flag
`advancedUi` is on. The mode is switched from a two-state control that reads
"Simple | Advanced" with the active side filled, placed in the right tray of
the main-window toolbar (top right), and from View > Advanced UI
(Ctrl+Shift+U); both write the flag at once and redraw the main window. In
Simple mode the deck list hides the Get Shared / Create Deck / Import File
row (Import stays under File; the Add window can still create a deck), the
deck menu (the gear next to a deck) has no RWKV submenu (Reschedule With
RWKV-Curve, Reschedule All Decks), Tools > Add-ons is shown only while at
least one add-on is installed (in Advanced mode it is always shown; the
entry is re-evaluated when the collection opens and when the mode changes),
and the deck-options screen shows its simplified view
(`spec/deck-options.md`, `deck-options.advanced-view`) with no switch of its
own. Hidden settings keep their stored values and keep taking effect.

**Why:** plan item 2 — the Simplified/Advanced split in the SuperMemo style,
Simple by default; Andrew, 2026-09-14, chose the toolbar placement with the
active side filled. The RWKV reschedule actions are power-user tools. A user
with add-ons installed must still reach them in Simple mode, even after
forgetting they are there; a user without any has no use for the entry.

**Pinned by:** `qt/tests/test_ui_mode.py` (toggle markup, click handling,
deck-browser row, the RWKV submenu, the Add-ons entry),
`advanced_ui_flag_is_reported` (`rslib/src/deckconfig/update.rs`).
