# User interface modes

## ui.mode-switch

Given a collection, the interface is in Simple mode unless the collection flag
`advancedUi` is on. The mode is switched from a two-state control that reads
"Simple | Advanced" with the active side filled, placed in the right tray of
the main-window toolbar (top right), and from View > Advanced UI
(Ctrl+Shift+U); both write the flag at once and redraw the toolbar and, on
the deck list, the bottom row from the tree already on screen. The switch
never recomputes the due counts: the mode does not affect dueness, so a
full main-window reset (which would rebuild the RWKV counts, slowly and
with "…" placeholders meanwhile) is not done. In
Simple mode the deck list's bottom row shows Find Decks Online (the button
formerly named "Get Shared") and Create Deck but not Import File (Import
stays under File); these buttons share one width in both modes, the
deck menu (the gear next to a deck) has no RWKV submenu (Reschedule With
RWKV-Curve, Reschedule All Decks), Tools > Add-ons is shown in both modes,
and the deck-options screen shows its simplified view
(`spec/deck-options.md`, `deck-options.advanced-view`) with no switch of its
own. Hidden settings keep their stored values and keep taking effect.

**Why:** plan item 2 — the Simplified/Advanced split in the SuperMemo style,
Simple by default; Andrew, 2026-09-14, chose the toolbar placement with the
active side filled. The RWKV reschedule actions are power-user tools. A user
with add-ons must reach them in Simple mode too (Andrew, 2026-09-15: the
entry is always shown; an earlier rule hid it while no add-on was
installed).

**Pinned by:** `qt/tests/test_ui_mode.py` (toggle markup, click handling,
deck-browser row, the RWKV submenu, the switch redrawing without a full
reset),
`advanced_ui_flag_is_reported` (`rslib/src/deckconfig/update.rs`).

## ui.review-heatmap

Given the collection flag `reviewHeatmapEnabled` (on by default; the "Show
the review heatmap on the deck list and the deck overview" checkbox in
Preferences > Review, carried by `Preferences.Reviewing`), the deck list
shows a review heatmap under the deck tree (whole collection) and the deck
overview shows one under its counts table (the current deck and its
subdecks): a yearly calendar of reviews per day (manual reschedules, ease
0, are not reviews) with the due forecast in a second colour, previous /
today / next navigation, and four figures below it: daily average on active
days, share of days with activity since the first review, longest streak,
current streak (which counts only while the last active day is today or
yesterday). Clicking a past day opens the browser on `prop:rated=-N` (with
`deck:current` prefixed in the overview); clicking a future day searches
`prop:due=N`. With the flag off, nothing is drawn and nothing is computed.
Days are grouped in local time with the "next day starts at" hour applied.

This is the Review Heatmap add-on (Glutanimate, AGPLv3) made native with
its default look (lime colours, yearly overview); the add-on's options
dialog, colour and mode switches, stats-screen injection and contribution
links are not ported.

**Why:** Andrew, 2026-09-15: integrate the add-on natively, with an option
to disable it in Preferences.

**Pinned by:** `qt/tests/test_review_heatmap.py` (streaks, averages, the
day map, the disabled flag, the render cache, the browser search);
`test_update_collection_writes_the_review_heatmap_preference`
(`qt/tests/test_preferences.py`);
`review_heatmap_is_on_by_default_and_a_reviewing_preference`
(`rslib/src/preferences.rs`).
