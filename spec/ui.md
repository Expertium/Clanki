# User interface modes

## ui.global-marker

Given a deck-options setting that applies to the whole collection rather
than to one preset (marked `global` in its help entry), its label carries a
meridian-globe icon (`mdiWeb`, the Material Design "web" glyph) in the link
colour (`--fg-link`), with the "affects the entire collection" tooltip; the
help modal shows the same icon next to that setting's explanation.

**Why:** Andrew, 2026-09-14: the upstream `earth` glyph reads as an odd blob;
a blue meridian globe is the familiar "global" symbol.

**Pinned by:** markup only (`ts/routes/deck-options/GlobalLabel.svelte`,
`ts/lib/components/HelpSection.svelte`).

## ui.mode-switch

Given a collection, the interface is in Simple mode unless the collection flag
`advancedUi` is on. The mode is switched from a two-state control that reads
"Simple | Advanced" with the active side filled, placed in the right tray of
the main-window toolbar (top right), and from View > Advanced UI
(Ctrl+Shift+U); both write the flag at once and redraw the main window. In
Simple mode the deck list hides the Get Shared / Create Deck / Import File
row (Import stays under File; the Add window can still create a deck), and the
deck-options screen shows its simplified view (`spec/deck-options.md`,
`deck-options.advanced-view`) with no switch of its own. Hidden settings keep
their stored values and keep taking effect.

**Why:** plan item 2 — the Simplified/Advanced split in the SuperMemo style,
Simple by default; Andrew, 2026-09-14, chose the toolbar placement with the
active side filled.

**Pinned by:** `qt/tests/test_ui_mode.py` (toggle markup, click handling,
deck-browser row), `advanced_ui_flag_is_reported`
(`rslib/src/deckconfig/update.rs`).
