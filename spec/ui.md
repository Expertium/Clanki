# User interface modes

## ui.global-marker

Given a deck-options setting that applies to the whole collection rather
than to one preset (marked `global` in its help entry; today only the
Algorithm, `deck-options.scheduler-choice`), its label reads
"<name> (global)" and carries a meridian-globe icon (`mdiWeb`, the Material
Design "web" glyph) in the link colour (`--fg-link`), with the "Affects the
entire collection." tooltip; the help modal shows the same icon next to that
setting's explanation.

**Why:** Andrew, 2026-09-14: the upstream `earth` glyph reads as an odd blob;
a blue meridian globe is the familiar "global" symbol. 2026-09-15: a global
setting in deck options, where people look most, needs a visible mark.

**Pinned by:** markup only (`ts/routes/deck-options/GlobalLabel.svelte`,
`ts/lib/components/HelpSection.svelte`); `ts/tests/e2e/deck-options.test.ts`
checks the "(global)" label.

## ui.mode-switch

Given a collection, the interface is in Simple mode unless the collection flag
`advancedUi` is on. The mode is switched from a two-state control that reads
"Simple | Advanced" with the active side filled, placed in the right tray of
the main-window toolbar (top right), and from View > Advanced UI
(Ctrl+Shift+U); both write the flag at once, switch the toolbar control in
place (the toolbar is not reloaded, so the sync button keeps its "sync
needed" colour and its spinner) and, on the deck list, redraw the bottom row
from the tree already on screen. The switch
never recomputes the due counts: the mode does not affect dueness, so a
full main-window reset (which would rebuild the RWKV counts, slowly and
with "…" placeholders meanwhile) is not done. In
Simple mode the deck list's bottom row shows Find Decks Online (the button
formerly named "Get Shared") and Create Deck but not Import File (Import
stays under File) or Get Add-ons (`ui.get-decks-and-get-addons`); these
buttons share one width in both modes, the
deck menu (the gear next to a deck) has no RWKV submenu (Reschedule this
deck, Reschedule all decks) and no Advance or Postpone entries
(`ui.advance-postpone`), nor has the Browser's Cards menu, Tools > Add-ons
is shown in both modes,
and the
deck-options screen shows its simplified view
(`spec/deck-options.md`, `deck-options.advanced-view`). The deck-options
screen has the same "Simple | Advanced" control at the right end of its top
bar: a click switches the page between its two views at once, keeping any
unsaved changes, and writes the same flag through the main window, which
redraws as above; it does not wait for Save, and closing without saving
keeps the new mode. The Stats page has the same control at the top right of
its top bar, with the same effect: in Simple mode the page shows only the
Reviews, Card Counts and Retention graphs (the default of
`ui.split-configurable`), in their usual order; Advanced mode shows every
graph. The note editor has the same control
at the right end of its toolbar row, and Simple mode there hides a part of
the toolbar buttons (`ui.editor-simple-view`). Each of these pages takes the
mode when it loads and from its own switch. Hidden settings keep their stored values and
keep taking effect. The control keeps its size when the mouse is over either
side: hovering changes only the background.

**Why:** plan item 2 — the Simplified/Advanced split in the SuperMemo style,
Simple by default; Andrew, 2026-09-14, chose the toolbar placement with the
active side filled. 2026-09-15: deck options get the switch too, always in
step with the main window's, so switching needs no closing and reopening of
deck options. Later the same day: the Stats page gets it too, and Simple
mode there shows only Reviews, Card Counts, Retention and Total Knowledge;
and the toolbar control switches in place (Andrew: "in place it is"): the
reload before cleared the sync button's colour and spinner until the next
redraw. The RWKV reschedule actions are power-user tools. A user
with add-ons must reach them in Simple mode too (Andrew, 2026-09-15: the
entry is always shown; an earlier rule hid it while no add-on was
installed). 2026-09-16, Andrew: hovering the switch on the Stats page and in
deck options made it "pop out" by a pixel, which he reported as a bug.

**Pinned by:** `qt/tests/test_ui_mode.py` (toggle markup, click handling,
deck-browser row, the RWKV submenu, the switch redrawing without a full
reset), `qt/tests/test_advance_postpone.py` (the Advance and Postpone
entries),
`advanced_ui_flag_is_reported` (`rslib/src/deckconfig/update.rs`);
`test_deck_options_mode_switch_sets_the_main_window_mode`
(`qt/tests/test_ui_mode.py`); "the deck-options switch changes the view at
once" (`ts/tests/e2e/deck-options.test.ts`); `graphs_report_the_ui_mode`
(`rslib/src/stats/graphs/mod.rs`); "Simple mode keeps only the Simple
graphs, in page order" (`ts/routes/graphs/ui-mode.test.ts`);
"Simple mode shows only the Simple editor buttons, in toolbar order"
(`ts/routes/editor/ui-mode.test.ts`); "the Simple | Advanced switch keeps its
size under the mouse" (`ts/tests/e2e/ui-mode-switch-hover.spec.ts`).

## ui.split-configurable

Given a collection, what Simple mode shows is decided item by item: each
part of the interface that Simple mode may hide is one item of a registry
(`qt/aqt/ui_split.py`: a stable id, an English name, its area and group, and
whether Simple mode shows it by default). Advanced mode shows every item,
whatever the choices. Simple mode shows an item when the user's choice for
it says so, else by its default. The defaults are the split the other
entries of this file describe (`ui.simple-mode-tools-hidden`,
`ui.get-decks-and-get-addons`, `ui.mode-switch`'s deck menu,
`ui.advance-postpone`, `ui.simple-mode-deck-counts`,
`ui.reviewer-simple-view`, `ui.browser-simple-view`, `ui.editor-simple-view`,
and `ui.mode-switch`'s Stats graphs), so a collection with
no choices looks exactly as those entries say.

The items, per area:

- Main window: the Tools menu (Study Deck, Create Filtered Deck, Check
  Database, Check Media, Empty Cards, Add-ons, Manage Note Types, Check for
  Updates); the deck list's buttons (Find Decks Online, Get Add-ons, Create
  Deck, Import File); the deck menu (Rename, Options, Advance/Postpone, the
  RWKV submenu, Export, Delete); the deck screen's buttons (Options,
  Rebuild, Empty, Custom Study, Unbury, Description); and the Learn count of
  the deck list and the deck screen (hidden, it is added to Due, as
  `ui.simple-mode-deck-counts` says).
- Reviewer: every entry of the More menu (the Flag Card submenu is one
  item).
- Browser: every entry of the Edit, Notes, Cards and View menus that
  `ui.browser-simple-view` names, the Go menu as a whole, the Flag and
  Layout submenus, Advance/Postpone, the Cards/Notes switch beside the
  search bar, the sidebar's Select tool and its seven sections, and each of
  the 19 Cards-mode columns. Simple mode's columns are the chosen ones, in
  the registry's order (Sort Field, Deck, Due, Interval, Retrievability,
  then the others);
  with none chosen, Sort Field shows. Without the Cards/Notes switch, the
  table shows cards (`ui.browser-simple-view`).
- Note editor: each of its own 17 toolbar buttons (`ui.editor-simple-view`);
  a hidden button is hidden from sight only, so its shortcut still runs it.
- Stats: each of the 18 graphs. In Simple mode the page asks the backend
  only for the data its chosen graphs draw, in page order (by default
  Reviews, Card Counts and Retention, as before); when the chosen graphs
  draw none of it (each asks for its own data), it asks for Card Counts'
  data only.
- Deck options: each setting (58), as `deck-options.simple-view` lists
  them: the Simple section's seven (shown by default) and every other one
  (Advanced-only by default). Where Simple mode draws an added setting is
  in that entry.

The editor, the Stats page and deck options read the split when they load, from the
media server (`getUiSplit`: every item id and whether Simple mode shows it,
the choices applied), as they read the mode; a page that cannot read it
shows every item. An edit of the split reaches such a page the next time
it is opened.

Never items, and so always shown: the Simple | Advanced switch and View >
Advanced UI, Tools > Preferences, and the UI split tab itself, so no choice
can lock the user out. Add-on menu entries, buttons and deck-list buttons
are not items and are never hidden.

The choices are edited in Preferences > UI split: one collapsible group per
area (and per menu or part within it), one "Show in Simple mode" checkbox
per item, a search field that keeps the items whose name, group or area
contain every word typed, and "Reset to defaults" (after a confirmation).
Above the list sits the one setting of the tab that is not a checkbox and
not an item of the registry: nothing; what names retrievability is no item
at all (`ui.retrievability-advanced-only`).
A change is stored at once and the open main window and Browser update in
place, the same way as for a mode switch (`ui.mode-switch`); no due count
is recomputed. The choices live in the collection config under `uiSplit`,
as a map item id -> true/false holding only the choices that differ from
the defaults, so they sync with the collection and an item added later
gets its default. A choice for an id this version does not know is kept,
also by Reset. On loading a collection the Tools menu and View > Advanced
UI are set from its mode and choices.

A hidden menu entry is taken out of its menu and held by its window, so its
keyboard shortcut still runs it (Tools, Browser); the reviewer and the deck
screen bind their shortcuts apart from the menu and the buttons, so theirs
work too. A hidden item's feature keeps working.

**Why:** Andrew, 2026-09-19: "Fully modular UI. If someone wants to, they
can treat Simple mode as 'stuff I use frequently' and treat Advanced mode
as 'stuff I never use so I just shoved it away'." He chose a Preferences
tab of its own, defaults equal to the fixed split, choices in both
directions, storage in the collection config as differences only, and the
switch, Preferences and the tab always visible. A choice can therefore
break the subset rule's usual shape only towards Advanced: Advanced still
shows every item, so Simple stays a subset (`CLAUDE.md`, "Simple mode is a
subset of Advanced").

**Pinned by:** `qt/tests/test_ui_split.py`
(`test_every_default_equals_todays_split` and the storage, lookup, area and
Preferences-tab tests, and the checks that the editor and Stats pages list
the same items in the same order); `test_hidden_tools_items_keep_their_shortcuts`
(`qt/tests/test_ui_mode.py`); "Simple mode follows the split in both
directions" (`ts/routes/editor/ui-mode.test.ts`,
`ts/routes/graphs/ui-mode.test.ts`, `ts/routes/deck-options/ui-split.test.ts`).

## ui.get-decks-and-get-addons

Given the deck list's bottom row, "Find Decks Online" opens
`https://ankiweb.net/shared/decks/` as before (upstream calls the
equivalent button "Get Shared"); a second button, "Get Add-ons", shown in
Advanced mode only, opens the same Install add-on dialog as the existing
Tools menu path (Add-ons, then Get Add-ons), unchanged. Simple mode does not
show "Get Add-ons". Like the other deck-list buttons, its label has no
trailing "..." (the Add-ons dialog's own button keeps it).

**Why:** upstream issue https://github.com/ankitects/anki/issues/5649: a
single "Get Shared" (here, "Find Decks Online") button covers both decks and
add-ons, which is unclear ("shared what?"). Splitting it in two, one action
each, removes the ambiguity; the second button is Advanced-only because
installing add-ons is a power-user action and Tools > Add-ons already
reaches it in both modes, so Simple mode is not losing capability
(`CLAUDE.md`, "Simple mode is a subset of Advanced" — this button set is a
strict superset in Advanced, a strict subset in Simple, of the same two
actions).

**Pinned by:** `test_deck_list_shows_get_addons_in_advanced_mode_only`,
`test_get_addons_button_opens_the_existing_install_dialog`
(`qt/tests/test_deckbrowser.py`).

## ui.simple-mode-deck-counts

Given Simple mode (`ui.mode-switch`), the deck list and the deck overview
show two counts, New and Due, where Due already includes Learn; Advanced
mode shows all three, New, Learn and Due. This is a display change only:
`Collection.sched.counts()`, the deck tree's `new_count` / `learn_count` /
`review_count`, and every other reader of those three numbers keep them
separate; only the HTML that shows them sums Learn into Due, and only in
Simple mode. Simple mode's Due column and row are the same control as
Advanced's, carrying a summed value, not a different control (so the
subset rule, `CLAUDE.md` "Simple mode is a subset of Advanced", holds:
Simple loses a column, it does not gain one). Switching the mode redraws
the deck list's tree and the overview's counts table in place, from state
already on screen, the same way the deck list's bottom row already does
(`ui.mode-switch`): no due count is recomputed.

**Why:** Andrew, 2026-09-17: "in Simple UI mode, in the main menu do not
show Learn and only show Due, with Learn numbers added to Due numbers" —
Learn/relearn queues are a scheduling detail a Simple-mode user does not
need to see split out.

**Pinned by:** `test_deck_list_header_hides_learn_column_in_simple_mode`,
`test_deck_list_header_shows_learn_column_in_advanced_mode`,
`test_deck_row_sums_learn_into_due_in_simple_mode`,
`test_deck_row_keeps_learn_and_due_separate_in_advanced_mode`,
`test_rwkv_deck_count_update_sums_learn_into_due_in_simple_mode`
(`qt/tests/test_deckbrowser.py`); `test_overview_table_sums_learn_into_due_in_simple_mode`,
`test_overview_table_keeps_learn_and_due_separate_in_advanced_mode`,
`test_overview_mode_redraw_repaints_the_page_and_the_bottom_bar`
(`qt/tests/test_overview.py`).

## ui.simple-mode-tools-hidden

Given Simple mode (`ui.mode-switch`), the Tools menu hides Create Filtered
Deck..., Check Database, Check Media, Empty Cards... and Manage Note Types.
Study Deck..., Add-ons, Check for Updates and Preferences stay in both
modes; an installed add-on's own menu entries are untouched (an add-on may
add entries anywhere in the menu; hiding them is not ours to do). The deck
overview's bottom bar loses the Custom Study button in Simple mode; Options
and Description stay in both modes. Switching the mode updates both places
in place, the same cheap way as the deck list's bottom row
(`ui.mode-switch`): no due-count recompute. A hidden item is hidden, not
disabled — its keyboard shortcut (`c` for Custom Study, F for Create
Filtered Deck, Ctrl+Shift+N for Manage Note Types) still runs it, same as
`ui.editor-simple-view`'s hidden editor buttons. These are the defaults of
`ui.split-configurable`.

Check for Updates stays visible in both modes as-is; two known, separate
defects in the check itself (`spec/updates.md`,
`updates.release-source` and `updates.dev-build-always-offered`) are out of
scope for this entry.

**Why:** Andrew, 2026-09-17: these are power-user tools a Simple-mode user
does not need and should not be confused by; Custom Study is an
Advanced-only feature of the overview screen for the same reason. Simple
mode hides items rather than replacing them, keeping it a strict subset of
Advanced (`CLAUDE.md`, "Simple mode is a subset of Advanced").

**Pinned by:** `test_tools_menu_hides_power_user_items_in_simple_mode`,
`test_tools_menu_keeps_shared_items_in_both_modes`,
`test_switching_the_mode_updates_the_tools_menu`
(`qt/tests/test_ui_mode.py`); `test_overview_bottom_bar_hides_custom_study_in_simple_mode`,
`test_overview_mode_redraw_repaints_the_page_and_the_bottom_bar`
(`qt/tests/test_overview.py`).

## ui.browser-simple-view

Given Simple mode (`ui.mode-switch`), the Browser window shows only what is
needed to find a card, fix it, and choose whether it is studied:

- Edit keeps Undo, Redo, Select All and Close; Select Notes, Invert
  Selection and Create Filtered Deck are Advanced-only.
- Notes keeps Add Notes, Delete and every tag item (Add Tags, Remove Tags,
  Clear Unused Tags, Toggle Tag); Create Copy, Export Notes, Change Note
  Type, Find Duplicates, Find and Replace and Manage Note Types are
  Advanced-only.
- Cards keeps Change Deck, Toggle Suspend and Info; the Flag submenu, Set
  Due Date, Grade Now, Reset, Reposition and Toggle Bury are Advanced-only
  (Advance and Postpone already are, `ui.advance-postpone`).
- View keeps Full Screen, Toggle Sidebar and the zoom items; the Layout
  submenu and the Cards/Notes toggle are Advanced-only. The Go menu is
  Advanced-only as a whole.
- The table's right-click menu follows the Cards and Notes menus.
- The Cards/Notes switch beside the search bar and the sidebar's Select tool
  are Advanced-only; in Simple mode the table shows cards, and a Browser that
  was left in notes mode switches to cards mode (remembered) when it opens or
  when the mode becomes Simple.
- The sidebar has no Saved Searches, Flags or Note Types sections (so no
  note type, card type or field actions); Today, Card State, Decks and Tags
  stay.
- Cards mode shows a fixed set of columns: Sort Field, Deck, Due and
  Interval (Retrievability is Advanced-only,
  `ui.retrievability-advanced-only`), with their own widths; a right-click on the
  column header does
  nothing. Advanced mode keeps the user's own column choice and widths, and
  switching the mode never changes them.

Every hidden menu item and the Select tool keep their keyboard shortcuts,
the same rule as `ui.simple-mode-tools-hidden`; the window holds them. An
add-on's own menu entries are untouched. Switching the mode updates an open
Browser in place, with the same search and selection. This list is the
default of `ui.split-configurable`, where the user can change it.

**Why:** Andrew, 2026-09-19: "There is a lot of stuff that most Anki users
will never touch", and he approved this list as proposed, with hidden items
keeping their shortcuts ("Ok"). Tags stay in Simple mode because the users
he asked voted for it. The Retrievability column joined the Simple set once
it showed the collection's own algorithm's value under RWKV too
(`ui.browser-memory-columns`; Andrew, 2026-09-19).

**Pinned by:** `qt/tests/test_browser_simple_view.py`
(`test_simple_mode_takes_the_advanced_only_items_out_of_the_menus`,
`test_hidden_items_keep_their_shortcuts`,
`test_an_addon_menu_entry_stays_through_a_mode_switch`,
`test_simple_mode_shows_fixed_columns_and_keeps_the_stored_choice`,
`test_saved_searches_flags_and_note_types_are_advanced_only`).

## ui.browser-remove-leech-tag

Given one or more selected cards in the Browser, Notes > Remove Leech Tag
(right after Remove Tags, and in the table's right-click menu) removes the
tag `leech` from their notes, the tag the scheduler adds when a card becomes
a leech. It changes nothing else: a card that was suspended as a leech stays
suspended, and other tags stay. Like every tag item it is shown in both
Simple and Advanced mode (`ui.browser-simple-view`), and it is disabled when
no card is selected.

**Why:** Andrew, 2026-09-19, passing on a user's request: "Remove Leech Tag"
in the card Browser when selecting cards.

**Pinned by:** `qt/tests/test_browser_simple_view.py`
(`test_remove_leech_tag_follows_remove_tags_in_both_modes`,
`test_remove_leech_tag_removes_only_the_leech_tag`).

## ui.reviewer-simple-view

Given Simple mode (`ui.mode-switch`), the reviewer's More menu keeps Suspend
Card, Options, Card Info, Tag Note, Delete Note, Replay Audio and Pause
Audio. Flag Card, Bury Card, Reset Card, Set Due Date, Previous Card Info,
Bury Note, Suspend Note, Create Copy, Audio -5s, Audio +5s, Record Own
Voice, Replay Own Voice and Auto Advance are Advanced-only. Every item keeps
its keyboard shortcut in both modes, as in `ui.simple-mode-tools-hidden`.
This split is the default of `ui.split-configurable`.

Flags are Advanced-only everywhere (this entry, `ui.browser-simple-view`):
Simple mode offers no way to set a flag or to list flagged cards except the
flag shortcuts, but a card's existing flag is still shown (the Browser row
colour, the reviewer's flag mark), so a flagged card never looks unflagged.
Tags stay in Simple mode everywhere. Marking a note adds the tag `marked`,
so the English labels call it tagging: "Tag Note" in the reviewer and
"Toggle Tag" in the Browser; the tag itself is still named `marked`.

**Why:** Andrew, 2026-09-19, choosing the items from the More menu, and:
"Make all flag stuff Advanced-mode and all tag stuff Simple mode" (tags
already do what flags do, so flags are redundant for most users); "Rename
Mark to Tag and keep it in simple mode"; "Set Due Date should be
Advanced-only"; "hide Suspend Note"; and he agreed that existing flags stay
visible.

**Pinned by:** `qt/tests/test_reviewer_simple_view.py`
(`test_simple_mode_more_menu_keeps_only_the_everyday_items`,
`test_advanced_mode_more_menu_is_unchanged`,
`test_marking_is_called_tagging`), and for the Browser's flags
`qt/tests/test_browser_simple_view.py`.

## ui.editor-simple-view

Given the note editor (Add, Edit Current, the Browser's editing pane) in
Simple mode (`ui.mode-switch`), its toolbar shows Fields..., Bold, Italic,
Underline, the text colour, Remove formatting and Attach pictures/audio/video,
and hides Cards..., the editor's own settings gear, Superscript, Subscript,
the text highlight colour, Unordered list, Ordered list, Alignment, Record
audio and Equations (MathJax/LaTeX): this is the default of
`ui.split-configurable`, where the user can move any of these buttons.
Advanced mode shows every one of them.
The buttons an add-on adds (`editor_did_init_buttons`,
`editor_did_init_left_buttons`) show in both modes. The field list, the
audio play buttons inside the fields and the Tags row show in both modes.
A hidden button stays in the page and is only hidden from sight, so its
keyboard shortcut still runs it (Ctrl+L for Cards..., Ctrl+= and Ctrl+Shift+= for
Superscript and Subscript, Ctrl+, and Ctrl+. for the lists, Ctrl+Shift+, and
Ctrl+Shift+. for the indents, F5 for Record audio, the Ctrl+M and Ctrl+T
combinations for the equations), its entry stays in the Remove formatting
list, and the format it registers keeps reading and writing existing HTML.
The switch sits at the right end of the toolbar row and changes the toolbar
at once: the note is not reloaded and text the user has typed is kept.

**Why:** plan item 2 — the Simplified/Advanced split, Simple by default.
Andrew, 2026-09-16, chose this list: a beginner writes text, colours it,
attaches media and tags the note; card templates, the block formatting and
MathJax are power-user tools. Tags stay in both modes because tags are data,
and Anki itself writes the leech tag. Add-on buttons stay because Clanki must
not hide a feature the user installed on purpose. Hiding a button is a UI
change, not a behavior change, so the shortcut and the feature keep working.

**Pinned by:** "Simple mode shows only the Simple editor buttons, in toolbar
order", "Advanced mode shows every editor button", "the Advanced-only buttons
are the ones Simple drops" (`ts/routes/editor/ui-mode.test.ts`); "the editor
switch changes the toolbar at once and keeps the typed text", "a hidden
button keeps its shortcut", "add-on buttons show in both modes"
(`ts/tests/e2e/editor-ui-mode.spec.ts`).

## ui.spin-box-steps

A spin box stores whole steps of its own step size. When the user types a
value between two steps, the box rounds it to the nearest step and stores
that; it never shows one number and stores another.

Desired retention steps by 1%, so it holds whole percents: typing 65.3 gives
65%, and typing 65.7 gives 66%. The deck-options "First intervals" table
names the two retentions in whole percents as well, "Current DR (65%)" and
"Selected DR (65%)".

A value that is already on a step does not move, and a box whose step cannot
round, such as a step of zero, leaves the value alone.

**Why:** Andrew, 2026-09-20: "Make sure desired retention is rounded to 1%
and the user cannot enter something like 65.3% DR", and "don't show .00".
The box already displayed as many decimal places as its step, so 65.3%
showed "65%" while the collection stored 0.653, and the table printed
"65.00%".

**Pinned by:** `desired retention keeps whole percents`,
`a value already on a step does not move`,
`whole-number boxes stay whole numbers`
(`ts/lib/components/spin-box.test.ts`).

## ui.process-name

On Windows, a source build appears in Task Manager as "Clanki", not as
"Python". The build writes `out/pyenv/Scripts/Clanki.exe`: a copy of the
virtual environment's interpreter whose description resource reads Clanki
instead of Python. Running it runs the interpreter, from the same virtual
environment, with the same arguments.

The name is written in place, so the copy has the same size as the
interpreter, byte for byte apart from the twelve bytes of the name. It lives
in the virtual environment's own directory, because the interpreter finds
`pyvenv.cfg` beside itself and refuses to start anywhere else. The installed
build is unaffected; its executable is named by the installer.

Writing the copy is not enough: something has to start it. **Every launcher
starts the app with the copy**, and the app never changes the process it is
already running in. `run.bat` picks the copy when the build has written one,
and the Playwright harness (`qt/tests/launch_anki_for_e2e.py`) picks it
through `tools/clanki_launch.py`, which holds the choice so a launcher can
make it without importing the app. Three things leave the plain interpreter
in place and none of them is an error, because the name is cosmetic:
another platform, a run that is already the copy, and a build that has not
written the copy yet.

**No launcher replaces its own process.** `tools/run.py` handed over with
`os.execv` between 2026-09-21 and the same day's fix. On Windows `os.execv`
does not replace a process: it starts a new one and ends the caller, so
anything waiting on the app stopped seeing it. Measured: a parent's
`wait()` returned in 0.08 s with exit code 0 while the real app ran for six
seconds more. That broke the Playwright harness, which deletes the
temporary `ANKI_BASE` it seeded as soon as its child ends, so the app lost
its collection while starting and mediasrv never bound. The VS Code debug
configuration and `run.bat`'s own `|| exit /b 1` were fooled the same way.

**Why:** Andrew, 2026-09-20: "Clanki doesn't show up as a process named
'Clanki'. It should." Task Manager shows a process's description resource
rather than its file name, so a renamed copy of the interpreter still reads
"Python", and a separate launcher program would add a second process to the
list.

**Pinned by:** `test_the_copy_describes_itself_as_the_app`,
`test_the_copy_is_the_same_size_and_still_runs`
(`qt/tests/test_win_app_exe.py`);
`test_a_launcher_starts_the_app_under_its_own_name`,
`test_the_app_is_not_asked_to_find_itself`,
`test_a_build_without_the_copy_runs_unchanged`,
`test_other_platforms_are_left_alone`,
`test_no_launcher_replaces_its_own_process`,
`test_the_e2e_launcher_starts_the_app_under_its_own_name`
(`qt/tests/test_clanki_launch.py`).

## ui.retrievability-advanced-only

Given any screen, the interface names the chance of recalling a card now
"retrievability", and it shows that word only in Advanced mode
(`ui.mode-switch`). Wherever the word is shown as text that can carry a
hover, it is underlined and explains itself: hovering it (or giving it the
keyboard focus) shows "Probability of recall". On the web pages that is every
section title, graph subtitle and card-info row label that contains the word
(`RetrievabilityText.svelte`, drawn with the glossary style of
`deck-options.glossary-term`); in the Qt screens a label that shows it
underlines the word and has the explanation as its tooltip, and a column
header or a list choice that shows it has the explanation as its tooltip.
Text that cannot carry markup (an axis label drawn in SVG, a dropdown
choice, a graph's own tooltip, a help page) shows the word plain.

Everything that shows the word is Advanced-only, whatever the UI split says
and also when the split cannot be read. It is no item of the split
(`ui.split-configurable`), so Preferences > UI split does not list it and no
choice can give it to Simple mode:

| Screen             | Advanced-only                                                                                                                                 |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Stats              | the Stability, Retrievability, Total Knowledge, AUC-ROC, Calibration and Universal Metric+ graphs                                             |
| Browser            | the Retrievability column (Simple mode's fixed columns do not include it)                                                                     |
| Card info          | the Retrievability row and the forgetting curve                                                                                               |
| Deck options       | Review sort order, New card gather order, Minimum reviews per day (RWKV), the RWKV-Instant queue recommendation, the simulator's review order |
| Filtered deck      | "Cards selected by", for both filters; Simple mode keeps the stored order                                                                     |
| Advance / Postpone | the whole dialog (`ui.advance-postpone`)                                                                                                      |

A setting that is hidden keeps its stored value and keeps taking effect. Only
the display changes: the search syntax (`prop:r`), the column key
`retrievability`, the order of the cards, the config keys and every API name
stay as they are.

There is no setting for the word. The recall-wording choice that Preferences

> UI split had until 2026-09-23 is gone; a stored `recallWording` value is
> ignored, and its proto fields are reserved.

**Why:** Andrew, 2026-09-23: "Remove the switch, keep 'retrievability' but
everywhere (whereever possible) underline it so that it shows 'probability
of recall' on hover. And make sure no graphs/sort orders/anything that is
currently Simple mode only shows 'retrievability'. Basically, the goal is to
show settings/menus/stats containing the word 'retrievability' ONLY in
Advanced mode AND explain what it means via a cursor hover window". Asked
about the graphs whose text mentions the word, he chose to make the whole
graph Advanced-only (Total Knowledge included); asked about the settings
that offer retrievability orders, he chose to make the whole setting
Advanced-only.

**Pinned by:** `test_no_item_of_the_split_names_retrievability`,
`test_the_tab_has_no_wording_choice`,
`test_the_word_is_underlined_and_explained_on_hover`
(`qt/tests/test_ui_split.py`);
`test_the_effect_line_explains_retrievability_on_hover`
(`qt/tests/test_advance_postpone.py`);
`the_retrievability_column_explains_itself_on_hover`
(`rslib/src/browser_table.rs`);
`the_filtered_deck_orders_say_retrievability`
(`rslib/src/decks/service.rs`); "an Advanced-only graph never shows in Simple
mode" (`ts/routes/graphs/ui-mode.test.ts`); `ts/lib/tslib/retrievability.test.ts`.

## ui.advance-postpone

Given Advanced mode (`ui.mode-switch`) and a collection whose algorithm is
FSRS-7 or RWKV-Curve (`sched.one-global-algorithm`), the deck menu (the
gear next to a deck) has "Advance Cards..." and "Postpone Cards..." after
Options, for the deck and its subdecks, and the Browser's Cards menu (and
its context menu) has "Advance Cards..." and "Postpone Cards..." after
Grade Now, for the selected cards (disabled without a selection). In Simple
mode, and under RWKV-Instant, none of them shows. There is no keyboard
shortcut and no Tools menu entry.

Choosing one gathers the candidates in the background (with a progress
window; under RWKV-Curve also each card's stored curve from the RWKV
process) and then shows a dialog: how many cards can move and how many of
them are relatively safe to move (`sched.advance`, `sched.postpone`), the
add-on's warning that moving departs from the optimal schedule, how many
cards were left out and why (no forgetting curve in the collection's
algorithm; Postpone: already at the maximum interval), a spin box for the
number of cards (from 0 to all candidates; for a deck the safe count, at
most 10, as in the add-on; for a Browser selection every candidate of it),
and, updated as the number changes, the mean retrievability of those cards
at review without and with the move ("Mean retrievability at review:
90.0% → 93.5%", the word explained on hover,
`ui.retrievability-advanced-only`). OK (disabled at 0) moves the first that many cards in the
background as one undoable operation ("Advance Cards" / "Postpone Cards" in
Edit > Undo), and a tooltip reports how many moved and the same means,
computed on the days they got. With no candidates, a tooltip says there are
no cards to advance or postpone (and why cards were left out) instead of
the dialog.

**Why:** Andrew, 2026-09-15: integrate Advance and Postpone from the FSRS
Helper add-on. The deck menu and the Browser are where Clanki acts on a
deck or on chosen cards; they are Advanced-only because they depart from
the optimal schedule (the add-on's own warning) and are rarely needed, like
the RWKV reschedule actions.

**Pinned by:** `qt/tests/test_advance_postpone.py`.

## addons.fsrs-helper-blocked

Given the FSRS Helper add-on (AnkiWeb 759844606, a source install in a folder
named `fsrs4anki-helper`, `fsrs4anki_helper` or `fsrs_helper`, or any add-on
named FSRS Helper or FSRS4Anki Helper) installed and enabled at start-up,
Clanki disables it before add-ons load and, the first time only, tells the
user so once the profile is open (a flag in the profile manager's global
meta). Given the user enables that add-on in Tools > Add-ons, or installs it
anew, a message says that the add-on is not compatible with FSRS-7, the
version of FSRS that Clanki uses, and the add-on stays disabled; an update
of a copy already installed stays disabled without a message. Other add-ons
enable and install as before.

**Why:** Andrew, 2026-09-19: "if the user tries to enable the FSRS Helper
add-on, display a window that says it's not compatible with FSRS-7 and keep
the add-on disabled. Similar treatment to Heatmap add-on, different reason".
The add-on is written for the FSRS versions of official Anki. Its Advance and
Postpone are built in (`sched.advance-postpone-algorithm`).

**Pinned by:** `qt/tests/test_fsrs_helper_addon.py`
(`test_fsrs_helper_is_recognised_by_id_folder_or_name`,
`test_an_enabled_fsrs_helper_addon_is_disabled_at_start_up`,
`test_the_fsrs_helper_notice_is_shown_only_once`,
`test_enabling_the_fsrs_helper_addon_is_refused_with_a_message`,
`test_installing_the_fsrs_helper_addon_leaves_it_disabled`).

## ui.review-heatmap

Given the collection flag `reviewHeatmapEnabled` on — its value in a new
collection, so a new user gets the heatmap out of the box, while a
collection where the user turned it off keeps it off — Clanki
draws a review heatmap — the Review Heatmap add-on (Glutanimate, AGPLv3)
made native: a calendar of reviews per day with the due forecast in a
second colour, previous / today / next navigation, and four figures
(daily average on active days, share of days with activity since the first
review, longest streak, current streak, which counts only while the last
active day is today or yesterday). It shows under the deck list (the whole
collection, minus the excluded decks), on the deck overview (the current
deck and its subdecks) and above the legacy stats report (Shift+click
Stats; the chosen deck or the collection, over the last month, year or the
whole history). Days are grouped in local time with the "next day starts
at" hour applied. Clicking a past day opens the browser on
`prop:rated=-N` (with `deck:current` prefixed where the heatmap covers one
deck); a future day, `prop:due=N`.

Its settings are in Preferences > Review Heatmap, a tab of its own: the
on/off switch; the color scheme (lime, olive, ice, magenta, flame; magenta
by default) and the calendar mode (yearly overview or a continuous
nine-month timeline); where the calendar shows (main screen, deck screen,
stats screen) and whether the four figures show even where it is hidden;
a history limit and a forecast limit in days, and a date before which
reviews are ignored (these three apply to the main and deck screens; the
forecast never reaches more than 5 years ahead, 1,826 days, whatever the
limit or the stats screen's period, and its "no limit" setting reads "5
years"); to
exclude deleted cards and manual reschedules (ease 0) from the history (the
latter on by default); and decks left out of the main-screen heatmap, with
their subdecks. The settings are stored in the collection config under
`reviewHeatmap` and sync. Until they are first saved, the add-on's stored
settings are used (its `heatmap` collection config and profile entries),
except a color scheme left at the add-on's own default. A gear button at the
right of the heatmap's controls opens the tab — the same gear icon the deck
list draws for deck options (`imgs/gears.svg`), in the grey of the three
navigation buttons beside it, and drawn larger than them: the gear carries
more detail than an arrow, so at their size it reads as a smudge;
Shift+click on the gear cycles the
color scheme and Shift+click on "today" cycles the calendar mode. With the
switch off, or where neither the calendar nor the figures show, nothing is
computed.

Given the Review Heatmap add-on installed and enabled at start-up, Clanki
disables it before add-ons load (both would draw a heatmap) and, the first
time only, tells the user so once the profile is open; the notice is never
shown again (a flag in the profile manager's global meta). Given the user
enables that add-on in Tools > Add-ons, or installs it, a message says that
Clanki already has the Review Heatmap built in (settings in Preferences >
Review Heatmap), and the add-on stays disabled; other add-ons enable as
before.

**Why:** Andrew, 2026-09-15: integrate the add-on natively with all its
settings, in a Preferences tab of their own, magenta by default, and retire
the add-on with a one-time notice. Later the same day: a user who tries to
enable the add-on must be told that Clanki has this built in. Andrew,
2026-09-16: the settings button carried the add-on's own three-bar mark,
which does not say "settings" to a reader; it now shows the gear that the
rest of Clanki uses. 2026-09-20: "make the cog inside the rounded square
icon of Heatmap settings a little bigger".

**Pinned by:** `qt/tests/test_review_heatmap.py` (streaks, averages, the
day map, settings parsing and defaults, the carry-over from the add-on,
colors, modes and visibility, the stats-screen period and scope, the
render cache, the browser search, the Shift+click cycling, the settings
link, disabling the add-on, the one-time notice,
`test_the_settings_button_shows_the_deck_lists_gear`,
`test_the_gear_is_drawn_larger_than_the_navigation_icons`,
`test_a_new_collection_has_the_heatmap_on_and_keeps_a_stored_off`,
`test_enabling_the_review_heatmap_addon_is_refused_with_a_message`,
`test_installing_the_review_heatmap_addon_leaves_it_disabled`);
`test_update_collection_writes_the_review_heatmap_preference`
(`qt/tests/test_preferences.py`);
`review_heatmap_is_on_by_default_and_a_reviewing_preference`
(`rslib/src/preferences.rs`).

## ui.review-heatmap-fills-in

Given a screen that draws the review heatmap (the deck list, or a deck's
overview), Clanki draws the screen as soon as its own counts are ready and
leaves the heatmap out where its report is not computed yet. It computes
that report in a background step, and draws the screen again in place when
the report arrives, without counting the deck tree or the deck's cards a
second time. A report that cannot be computed leaves the screen as it is,
rather than being drawn again with nothing new. Once a report is cached,
the screen draws it at once and starts no background step. Nothing warms
a heatmap up ahead of the screen that shows it. Everything else about the
heatmap is unchanged (spec ui.review-heatmap): the same report, the same
cache, the same figures. The congratulations screen has no heatmap and
computes none.

**Why:** Andrew, 2026-09-21: "the first click on a deck has a MASSIVE
delay, like 1-3 seconds. After that everything is fine." The first click
took 719 ms, of which 635 ms was the deck overview's first heatmap report
(18 ms on every later click). A warm-up existed for exactly this, 2 s after
the deck list is drawn, but it is 2 s that a user often clicks inside: the
warm-up then took the one collection worker, and the click's counts waited
behind it. The screen waited for the heatmap before it drew anything, so
the user waited for a calendar to see the deck's counts. Both screens now
draw first: the first click is 72 ms, and its heatmap arrives about 650 ms
after it.

**Pinned by:** `test_the_deck_list_and_overview_draw_without_waiting_for_the_heatmap`,
`test_a_screen_without_its_heatmap_draws_it_in_the_background_and_again`,
`test_a_heatmap_that_cannot_be_computed_is_not_drawn_again`,
`test_a_ready_heatmap_is_drawn_at_once_with_no_background_step`,
`test_a_deck_opened_while_the_report_ran_gets_its_own_heatmap`,
`test_a_cold_cache_is_reported_without_reading_the_collection`,
`test_the_background_step_fills_the_cache_and_reports_an_error`,
`test_the_deck_list_no_longer_warms_the_overview_heatmap_after_2_s`
(`qt/tests/test_review_heatmap.py`).

## ui.review-heatmap-kept-counts

Given a collection whose heatmap was drawn in an earlier session, the next
session draws the first deck list and the first deck overview from counts
kept on disk, without a pass over the whole review log. The heatmap keeps
the two counts that need such a pass, the reviews per day and deck and the
reviews per day of the whole collection, both of the reviews before a
cut-off time, in the file `collection.heatmap-cache.json` beside the
collection. A kept count is used only when it still fits, by the same check
as within a session (`ui.review-heatmap`): the number of reviews before its
cut-off and the newest of them, the rollover hour, the time zone, the
heatmap's settings, and each deck's cards made before the cut-off. The
reviews after the cut-off are counted on every draw, as before. A kept count
whose cut-off is more than 7 days old is not used, so that part stays small;
a count made again replaces the kept one. A missing, damaged or other-version
file is ignored. The heatmap drawn is the same as with no kept counts.

**Why:** Andrew, 2026-09-24 (H5, PR #85): the per-deck counts made the first
deck of a session slower, "accept it, though I would appreciate if you
looked for a way to reduce the delay on the first deck". On a copy of his
collection (1.32M reviews, 103 decks) the per-deck counts took 883-915 ms and
the whole collection's 685-729 ms, at every start; reading them back takes
20-34 ms.

**Pinned by:** `test_the_next_session_draws_from_the_kept_counts`,
`test_kept_counts_that_no_longer_fit_are_made_again`,
`test_a_damaged_or_foreign_kept_file_is_ignored`,
`test_kept_counts_read_back_as_they_were`
(`qt/tests/test_review_heatmap.py`).

## ui.periodic-backup-waits

Given the periodic backup check (every 5 minutes while a profile is open),
Clanki skips the check while the reviewer is open (a card's question or
answer on screen, including Show Answer and the answer buttons) and while
anything else uses the collection: a background task queued or running on
the collection (such as the rescheduling after a deck-options save, an
RWKV-Curve reschedule or a sync) or a request from a web page (such as an
FSRS optimization started from deck options). A skipped check makes no
backup; the next check outside the reviewer with the collection free makes
it, under the usual rules (the minimum interval between backups, and only
when the collection changed). Backups made by the user (File > Create
Backup) and the backup on close are not affected.

**Why:** Andrew, 2026-09-15: a periodic backup holds the collection while it
copies it, so an answer, an optimization or a reschedule that comes at the
same time waits, and the "Processing..." window appeared during reviews.

**Pinned by:** `test_periodic_backup_waits_for_reviews_and_other_collection_work`,
`test_collection_busy_counts_queued_and_running_collection_tasks`
(`qt/tests/test_main.py`); `test_web_page_backend_request_counts_as_collection_use`
(`qt/tests/test_mediasrv.py`).

## ui.audio-starts-off-main-thread

Given a profile that opens, Clanki starts its sound player (mpv) on a thread
of its own, and the main window does not wait for it. Until mpv is ready, a
stand-in player takes its place: a sound that a card asks for in that time
waits and plays as soon as mpv is ready, and a sound that is stopped before
then does not play. mpv finishes its own setup on that thread (its event
callbacks, its version) before it takes over, so a waiting sound plays on a
player that is ready, and starting mpv writes nothing to the error output,
which Clanki would show as an error. When mpv cannot start (missing, too
old, or no answer within 10 s), the mplayer fallback takes over, the same as
before.

**Why:** starting mpv waits for its pipe: at least one 100 ms poll, and up to
10 s when the bundled mpv hangs at start on a busy machine (B-017: 12 hangs in
40 starts with 14 cores busy). That wait ran on the main thread before the
main window showed. Andrew, 2026-09-20: start-up must be near-instant, with
no heavy work before the window.

**Pinned by:** `test_setup_audio_does_not_wait_for_mpv`,
`test_a_sound_asked_for_while_mpv_starts_plays_when_it_is_ready`,
`test_mpv_that_cannot_start_falls_back_to_mplayer`,
`test_a_stopped_sound_does_not_play_when_mpv_is_ready`,
`test_mpv_that_is_ready_after_shutdown_is_closed`,
`test_mpv_started_off_the_main_thread_sets_itself_up_there`
(`qt/tests/test_sound.py`).

## ui.audio-mingw-mpv

Given a Windows x64 build of Clanki (the source build's `out/pyenv` and the
installer), the sound player is the MinGW build of mpv v0.41.0, from mpv's own
release (`mpv-v0.41.0-x86_64-w64-mingw32.zip`, pinned by its SHA-256), not the
MSVC build that anki-audio 0.2.3 ships. The build copies its `mpv.exe` and the
DLLs it needs into `anki_audio/mingw/`, and Clanki starts mpv from there when
that folder has one. anki-audio's own files are never written, because uv
hard-links them to its cache and so to every other environment that has
anki-audio. Both builds are the same mpv release (commit 41f6a6450), so what
mpv does with a sound does not change. Windows ARM, macOS and Linux keep the
mpv they had, and so does an environment built before this entry.

**Why:** B-017. The MSVC build deadlocks during start-up when the machine is
busy, and a hung start costs the user their sound until Clanki restarts.
Measured 2026-09-24 with 14 cores busy, 40 tries each: v0.41.0 MSVC hung in
31, v0.41.0 MinGW in 0, the 2026-09-23 nightly MSVC in 23, the nightly MinGW
in 0. On an idle machine the MSVC build hung in 3 of 30 and the MinGW build in
none. A newer mpv alone does not help; the MinGW build does. Andrew,
2026-09-24: "Use MinGW".

**Pinned by:** `test_the_mingw_mpv_goes_beside_anki_audios_files`,
`test_files_already_in_place_are_not_copied_again`
(`qt/tests/test_install_mingw_mpv.py`),
`test_install_mingw_mpv_into_bundle_adds_the_mingw_mpv`
(`qt/tests/test_installer.py`), `test_packaged_mpv_prefers_the_mingw_build`
and, on Windows x64, `test_the_built_environment_has_the_mingw_mpv`
(`qt/tests/test_sound.py`).

## ui.card-info-rwkv-curve

Given a card whose preset runs RWKV-Curve, card info's forgetting-curve chart
shows only RWKV-Curve's own curves. **The rule:** no FSRS-7 segment and no
FSRS-7 value is ever drawn on such a card, whatever RWKV has, unless the user
picks FSRS-7 in the toggle below. FSRS-7's parameters change nothing on the
RWKV-Curve chart.

**The toggle (Advanced mode).** In Advanced mode (`ui.mode-switch`) the chart
of such a card has a choice above it, "RWKV-Curve" and "FSRS-7", with
RWKV-Curve picked when card info opens. Picking FSRS-7 draws FSRS-7's curves
instead, exactly as for a card of an FSRS-7 preset: one segment per review,
from FSRS-7's own memory state after that review as the card's preset's FSRS-7
parameters give it from the card's history. It is not the memory state stored
on the card, which holds RWKV-Curve's S90. The chart never draws both
algorithms at once, and the choice changes nothing but the chart: the
"Stability" and "Retrievability" rows stay RWKV-Curve's. Simple mode has no
toggle. The page gets FSRS-7's reviews in their own list of the card-stats
response, filled only in Advanced mode under RWKV-Curve.

**The full history.** The chart draws one segment per answered review:
after each review, the curve RWKV held for the card right after that review,
up to the next review. That is the curve RWKV-Curve scheduled the card
with, the real answer's own curve and not a per-button probe. Clanki cannot
recompute it later from the card's own reviews, because RWKV's state depends
on every earlier review of the collection, so the RWKV replay (the history
pass at start-up and the one that records the Stats data) and each answer in
the reviewer save a compact source of that curve for every answered review,
and card info rebuilds the curve from it. The source's format belongs to the
model and is tagged with it: the current model saves the 128 values its
curve head reads, as 16-bit floats (about 256 bytes per review). Each saved
source carries the model's identity (the SHA-256 of its weights) and the
format and kernel versions; a source whose tag does not match the running
model is stale and is never drawn. The sources live in the
`collection.retrievability-cache.sqlite` file beside the collection, with the
other per-review recordings: a cache, never synced, never read by official
Anki or AnkiDroid.

Opening card info for such a card is one of the two things that start the
recording pass that saves those sources (`sched.rwkv-recordings-automatic`);
the other is opening Stats. A card's own past curves cannot be worked out
from the card alone, because RWKV's state after one review depends on every
review of the collection before it, so nothing but that pass can fill the
chart.

A review with no saved source (older than the recording, recorded by
another model, or a review the RWKV replay does not read) gets no segment: the line breaks there, no curve is invented
for it, and no FSRS-7 curve stands in. The chart starts at the oldest review
that has a source; with none, it starts at the last answered review.
The segment after the last answered review is the curve RWKV holds for the
card now; the rebuilt curve of that review matches it to within 16-bit
rounding.

The last segment runs from the last answered review to now and then as a
dashed preview. The chart's tooltip, card info's "Stability" row and the
latest review's stability in the page data show that curve's S90 (where it
meets 90% recall), not the S90 stored on the card. While RWKV has no curve
for the card (its state still loading, busy, or no answered review), the
chart shows no data and card info has no "Stability" row. A curve reaches
the page as recall at 0 and at 300 elapsed times evenly spaced in log time
from one minute to 100 years, joined by straight lines; every segment uses
the same elapsed times, and its tooltip shows that segment's own S90. The
chart draws whether or not FSRS-7 has a memory state for the card
(`ui.card-info-curve-messages`).

Card info for such a card shows no other FSRS-7 value either: no
"Difficulty" row, and its one "Retrievability" row is the curve's recall
now (`ui.card-info-one-algorithm`). The page data of the reviews before the latest answered one carries no
FSRS-7 memory state, so no FSRS-7 stability of those reviews reaches the page.
Cards of FSRS-7 presets draw FSRS-7's curve with its S90 for every review;
RWKV-Instant cards draw none (`ui.card-info-one-algorithm`). With no curve the
box says why (`ui.card-info-curve-messages`).

**Why:** Andrew, 2026-09-15: forgetting curve graphs always use the S90, for
RWKV-Curve as for FSRS-7; the Stability row and the tooltip show the drawn
curve's S90; with no RWKV curve, hide the segment; never mix two algorithms
in one display, in general (so no FSRS-7 segments or values in an
RWKV-Curve card's info); and the older reviews of an RWKV-Curve card show
no FSRS-7 S90. He also asked, on the same day, that the forgetting curve
"always shows the full history of a card, all reviews", for both algorithms,
and on 2026-09-16, shown the one-segment chart: "that is absolutely not
intended whatsoever". His no-mixing rule forbids borrowing FSRS-7's curve;
it says nothing about RWKV's own. On 2026-09-19 he accepted about 170 MB of
saved sources for his 656k reviews, so that the chart can draw them. On
2026-09-23 he asked for the toggle: "in Advanced UI mode, show a FSRS-7 /
RWKV-Curve toggle for the forgetting curve graph, so that I can look at curves
of both algorithms in Card Info"; one algorithm at a time keeps two
algorithms out of one display.

**Pinned by:** `an_rwkv_curve_card_carries_fsrs7s_own_states_only_in_advanced_mode`
(`rslib/src/stats/card.rs`); "the curve toggle draws one algorithm at a time"
(`ts/routes/card-info/forgetting-curve.test.ts`);
`card_curve_points_are_the_curve_and_its_s90`,
`curve_sources_rebuild_the_curve_stored_at_each_review`,
`curve_sources_of_another_format_are_not_read`
(`rslib/src/rwkv/mod.rs`);
`rwkv_curve_sources_are_per_review_cache_rows_read_by_tag`
(`rslib/src/storage/revlog/mod.rs`);
`test_rwkv_card_info_curve_samples_the_stored_curve`,
`test_rwkv_card_info_curve_is_none_without_a_curve`,
`test_rwkv_card_info_rebuilds_the_saved_curve_of_each_review`,
`test_rwkv_curve_source_writer_tags_every_source_with_the_model`,
`test_rust_runtime_hands_each_chunks_curve_sources_with_review_ids`,
`test_live_answer_saves_its_curve_source_under_its_review_id`
(`qt/tests/test_rwkv_scheduler.py`);
`test_card_info_gets_rwkv_curves_own_curve_and_s90`,
`test_card_info_sends_rwkv_curves_of_the_earlier_reviews`,
`test_card_info_has_no_rwkv_curve_for_other_algorithms`,
`test_card_info_starts_the_recording_pass`
(`qt/tests/test_mediasrv.py`); "an RWKV-Curve card shows its curve's S90
and R, and no difficulty", "an RWKV-Curve card without a curve shows no
stability and a calculating R" (`ts/routes/card-info/lib.test.ts`);
"rwkvRecallAt interpolates between the
curve's points", "no FSRS-7 value reaches an RWKV-Curve card's chart",
"after the last review an RWKV-Curve card follows RWKV's curve and S90",
"an RWKV-Curve card draws RWKV's curve after every review that has one",
"a review without a stored RWKV curve gets no segment, and none is
invented", "without an RWKV curve yet the chart stops at the last review"
(`ts/routes/card-info/forgetting-curve.test.ts`).

## ui.card-info-curve-messages

Given card info's "Forgetting Curve" box with no curve to draw, it shows a
short message that says why, never the words "NO DATA", and never a value of
the other algorithm (`sched.one-global-algorithm`):

| Case                                                          | Message                                                                          |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| RWKV is not ready (state loading, or another thread holds it) | "Calculating…"                                                                   |
| RWKV-Curve answered but has no curve for the card             | "RWKV-Curve has no curve for this card yet. It gets one after your next answer." |
| the card has no answered review                               | "No curve yet. It appears after you answer this card."                           |
| FSRS-7 after a reset, with no answer since                    | "This card was reset. The curve appears after you answer it again."              |
| FSRS-7 with no review on a later day                          | "No curve yet. This card needs a review on a later day."                         |

While RWKV is not ready, the card-stats response marks its curve `pending` and
the card-info page asks for the card again every 2 seconds, so the curve
appears without the user closing and reopening card info.

An RWKV-Curve card draws from its latest answered review even when FSRS-7 has
no memory state for the card, a card that was reset for example: RWKV's stored
curve needs no FSRS-7 memory state (`ui.card-info-rwkv-curve`).

**Why:** Andrew, 2026-09-16: make sure no "NO DATA" appears; where no curve can
exist, say why in plain words; never fall back to the other algorithm; and a
card whose RWKV state is still loading gets its curve once the state arrives.

**Pinned by:** `test_card_info_marks_the_rwkv_curve_pending_until_rwkv_is_ready`
(`qt/tests/test_mediasrv.py`);
`test_rwkv_card_info_curve_result_is_pending_while_rwkv_is_not_ready`,
`test_rwkv_card_info_curve_is_none_without_a_curve`
(`qt/tests/test_rwkv_scheduler.py`); "a reset card still draws RWKV-Curve's
curve from its last answer", "the forgetting curve says why it has no curve,
and never says NO DATA" (`ts/routes/card-info/forgetting-curve.test.ts`).

## ui.fsrs7-no-rwkv-values

Given a collection whose algorithm is FSRS-7 (`sched.one-global-algorithm`),
nothing on screen comes from RWKV, even with an RWKV model loaded: the
reviewer runs no RWKV prediction for its cards, their card info has no RWKV
rows ("RWKV computed R", "Retrievability source", the answer-button
probabilities, "RWKV : R After Review"), and the Stats page prepares no RWKV
scores, so its Retrievability graph has no RWKV series (any scores left
from an earlier algorithm are dropped). Opening the Stats page does not
load the model.

**Why:** Andrew, 2026-09-15: never mix two scheduling algorithms in one
display; every place shows only the active algorithm's values.

**Pinned by:** `test_fsrs7_card_gets_no_rwkv_prediction_and_no_card_info_rows`,
`test_fsrs7_collection_prepares_no_rwkv_stats_scores`
(`qt/tests/test_rwkv_scheduler.py`).

## ui.card-info-one-algorithm

Given card info (the browser's sidebar, the Card Info window, the reviewer's
card info) for a card with an FSRS memory state, in Advanced mode it shows
only the values of the collection's algorithm (`sched.one-global-algorithm`)
and one retrievability, labelled "Retrievability", which explains itself on
hover (`ui.retrievability-advanced-only`):

| Algorithm    | Stability       | Difficulty | Retrievability         | Forgetting curve  |
| ------------ | --------------- | ---------- | ---------------------- | ----------------- |
| FSRS-7       | FSRS-7's S90    | FSRS-7's   | FSRS-7's               | FSRS-7's          |
| RWKV-Curve   | the curve's S90 | none       | the curve's recall now | RWKV-Curve's (\*) |
| RWKV-Instant | none            | none       | RWKV's prediction      | none              |

(\*) In Advanced mode a toggle above the chart can show FSRS-7's curves
instead, one algorithm at a time (`ui.card-info-rwkv-curve`).

RWKV-Curve's recall now is its stored curve (`ui.card-info-rwkv-curve`) at
the time since the card's latest answered review. While RWKV has no value
yet, the Retrievability row reads "Calculating…". Card info shows no other
RWKV rows: no second retrievability, no answer-button probabilities, no
next-S90 rows per button, no "R After Review" or "R After 10min", and no
retrievability source. In Simple mode (`ui.mode-switch`) card info shows no
stability, difficulty or retrievability at all, and no forgetting curve
(`ui.retrievability-advanced-only`).

**Why:** Andrew, 2026-09-15: "It's too much clutter, just remove all of this
and keep one R value"; "don't show DSR values in card info in Simple mode";
never mix two algorithms in one display.

**Pinned by:** `card_stats_report_the_algorithm_and_the_mode`
(`rslib/src/stats/card.rs`);
`test_card_info_queries_rwkv_without_cached_reviewer_prediction` (an
RWKV-Instant card's one row), `test_reviewer_rwkv_prediction_uses_reviews_of_other_cards`
(an RWKV-Curve card's none), `test_rwkv_card_info_curve_gives_the_recall_now`
(`qt/tests/test_rwkv_scheduler.py`); `test_card_info_gets_rwkv_curves_own_curve_and_s90`
(`qt/tests/test_mediasrv.py`); "FSRS-7 shows stability, difficulty and one
retrievability", "Simple mode shows no difficulty, stability or
retrievability", "an RWKV-Curve card shows its curve's S90 and R, and no
difficulty", "an RWKV-Instant card shows only RWKV's R, once, and no
forgetting curve" (`ts/routes/card-info/lib.test.ts`).

## ui.stats-one-algorithm

Given the Stats page, its graphs draw only the collection's algorithm
(`sched.one-global-algorithm`):

| Algorithm    | Retrievability graph    | Difficulty graph | Stability graph |
| ------------ | ----------------------- | ---------------- | --------------- |
| FSRS-7       | FSRS-7's R              | shown            | shown           |
| RWKV-Curve   | the RWKV-Curve head's R | none             | shown (S90)     |
| RWKV-Instant | RWKV-Instant's R        | none             | none            |

Under RWKV there is no FSRS-7 series beside RWKV's and no FSRS-7 value for
a card RWKV has not scored. While RWKV has not scored the page's search yet
(its state loading or warming up, or RWKV scoring the searched cards), the
Retrievability graph shows "Calculating…" instead of values, and the page
asks again every 2 seconds until the scores arrive. The page's other graphs
do not wait for RWKV's scores: they are drawn first, and the Retrievability
graph fills in when the scores arrive. Under FSRS-7 no RWKV score is
prepared at all (`ui.fsrs7-no-rwkv-values`). The RWKV-Curve R here is
each card's stored curve now (`ui.rwkv-curve-r-stored-curve`), the same value
card info shows (`ui.card-info-one-algorithm`).
The Retrievability graph names its series after the algorithm (FSRS-7,
RWKV-Curve or RWKV-Instant, never a bare "RWKV"), and a click on a bar opens
the Browser on that algorithm's own R: `prop:r`, `prop:rwkv-curve:r` or
`prop:rwkv:r`. Shift+click does the same; it no longer searches FSRS-7's R
under RWKV.

**Why:** Andrew, 2026-09-15: never mix two algorithms in one display; while
RWKV is not ready, show "…" or "Calculating…" rather than FSRS-7's values;
RWKV has no difficulty and RWKV-Instant no stability. 2026-09-16: RWKV
scoring every card of a big deck takes minutes (about 4 on two cores for
Andrew's 159k cards), during which the Stats page showed only its spinner.

**Pinned by:** `retrievability_graph_uses_rwkv_scores_for_matching_search`,
`fsrs7_stats_show_no_rwkv_values_and_rwkv_curve_uses_the_curve`
(`rslib/src/stats/graphs/retrievability.rs`);
`test_rwkv_curve_collection_active_reads_the_algorithm`
(`qt/tests/test_rwkv_scheduler.py`);
`test_graphs_leave_rwkv_retrievability_for_later_when_asked`
(`qt/tests/test_mediasrv.py`); "while RWKV calculates, the graph shows
and says so, with no other values" (`ts/routes/graphs/retrievability.test.ts`).

## ui.rwkv-curve-r-stored-curve

Given a collection that runs RWKV-Curve, a card's RWKV-Curve R is the
forgetting curve RWKV stored for the card at its last answered review (the
curve RWKV-Curve schedules the card with), evaluated at the time since that
review: the seconds since it, at least 1 second, else the whole days since
it in seconds. This one value is what these places read:

- the Stats Retrievability graph;
- the Browser's `prop:rwkv-curve:r…` searches, and AnkiConnect's searches
  with it; the Browser's Retrievability column and its sort
  (`ui.browser-memory-columns`);
- filtered decks whose searches use `prop:rwkv-curve:r…`;
- the RWKV-Curve review order by retrievability (`sched.rwkv-review-order`)
  for the cards those places scored.

The same stored curve decides `is:rwkv-curve:due` (the Browser, AnkiConnect,
filtered decks): a review card matches when the whole days since its last
review reach the curve's current interval at the card's target retention, the
interval the RWKV-Curve reschedule gives it (`sched.rwkv-curve-reschedule`). A
card with no stored curve does not match.

The value does not change when RWKV's shared states (deck, preset, global)
change after the card's last answer, for example when other cards of the
deck are answered; only an answer of the card itself stores a new curve. A
card RWKV stored no curve for gets no value, and no other value (not a new
query of RWKV, not RWKV-Instant's, not FSRS-7's) stands in for it.

Such a request does not run RWKV-Instant's rating head unless it also reads
it: a search with `prop:rwkv:r…` or `is:rwkv:due`, or a filtered deck ordered
by retrievability.

**Why:** Andrew, 2026-09-19: "Ok, switch to using the stored curve", and
the same day, for the Browser, filtered decks and AnkiConnect too ("option
A"). Before, these places ran a new query of RWKV for every card, which reads
the current shared states: on a copy of his collection (38,523 cards) that
value differed from the stored curve by 0.03 in the median card, 0.21 at the
99th percentile and up to 0.44, and card info and the graph disagreed. The
whole-collection scoring took 5.0 s with the query and takes 1.5 s now.
Andrew, 2026-09-24: "Yep, fix them", and the RWKV session's rule that an
interval comes from the curve of the card's last real review: before,
`is:rwkv-curve:due` compared the elapsed days with the interval of a new query
of RWKV, a curve training never gives a loss, so the search and the reschedule
could disagree about the same card on the same day.

**Pinned by:** `test_rwkv_curve_r_is_the_stored_curve_now`,
`test_rwkv_curve_r_request_runs_the_rating_head_only_for_its_readers`,
`test_prepare_stats_scores_asks_for_the_rating_head_only_when_read`,
`test_rwkv_curve_r_publishes_cards_without_the_rating_head`,
`test_stats_curve_due_reads_the_stored_curve_interval`,
`test_filtered_deck_curve_due_uses_current_curve_interval`
(`qt/tests/test_rwkv_scheduler.py`);
`reschedule_intervals_come_from_the_stored_curve` (`rslib/src/rwkv/mod.rs`)

## ui.rwkv-no-prediction-never-rated

Given a collection that runs RWKV-Instant and a review card that was never
rated (its history has no answered review, for example after Set Due Date on a
new card, or a card imported without its reviews), RWKV has no prediction for
the card:

- card info's Retrievability row reads "No prediction", and card info does not
  query RWKV for the card;
- every reader of a search gets no value for it: the Browser's Retrievability
  cell stays blank, the Stats Retrievability graph leaves it out, `prop:rwkv:r…`
  and `is:rwkv:due` searches (the Browser, AnkiConnect, filtered decks) do not
  match it, and a filtered deck ordered by retrievability puts it with the cards
  that have no value (`sched.filtered-deck-one-algorithm`).

The study queue of a normal deck and its review count still score the card with
a query of RWKV-Instant, as before: a review card without a score is never
gathered (`sched.rwkv-instant-waits`), so no score there would keep the card out
of study until it is rated somewhere else.

Under RWKV-Curve such a card has no stored curve, so it has no value already
(`ui.rwkv-curve-r-stored-curve`).

**Why:** Andrew, 2026-09-24: "Yep, fix them", with the RWKV session's rule that
a never-rated review card shows "no prediction" instead of a frozen R. Before,
RWKV fed such a card the "no previous review" sentinel as its elapsed time and
no card state, so its R did not change with time, and training has no query row
like it. The study queue is left as it was until Andrew decides what it should
do with such a card.

**Pinned by:** `never_rated_review_cards_get_no_row_for_a_search`
(`rslib/src/scheduler/rwkv.rs`);
`test_card_info_says_no_prediction_for_a_never_rated_review_card`
(`qt/tests/test_rwkv_scheduler.py`)

## ui.stats-rwkv-scores-kept

Given the Stats page asks for the Retrievability graph again while nothing
RWKV reads has changed — the same collection and RWKV backend, the same
search, the same day, the same RWKV state generation, the same review inputs
and study queues — Clanki reuses the score map it published before instead of
scoring every card again, while the map is fresh (`sched.rwkv-r-freshness`):
for the shortest time tolerance of its cards. A new day, an answer, any
change of the cards or the queues, another search or deck, a new RWKV state
generation, another backend or collection and any other publication of a
score map end it at once, and the next request scores again.

**Why:** Andrew, 2026-09-16: switching the Stats page between Simple and
Advanced mode, or its period between 12 months and all history, must not
start the RWKV calculation from zero again. RWKV's answer depends on neither
the mode nor the period. The time limit is the
freshness rule Andrew approved on 2026-09-19 (`sched.rwkv-r-freshness`); a
whole-collection pass now costs about 2 seconds.

**Pinned by:** `test_prepare_stats_retrievability_scores_reuses_published_scores`,
`test_prepare_stats_retrievability_scores_reuse_ends_with_the_time_tolerance`,
`test_prepare_stats_retrievability_scores_scores_again_for_another_search`,
`test_prepare_stats_retrievability_scores_scores_again_after_a_new_day`
(`qt/tests/test_rwkv_scheduler.py`)

## ui.stats-scoring-cancelled

Given the Stats window closes while a Stats graphs request is still scoring
cards with RWKV for the Retrievability graph, the scoring stops at its next
batch boundary. It publishes no score map, so the map the collection already
holds is left as it is, and the remembered key of that map
(`ui.stats-rwkv-scores-kept`) is dropped, so the next request scores again.
The stop happens between two batches, which is outside the RWKV lock, so the
lock is free as soon as the running batch ends. A Stats request that starts
after the window closed is not cancelled, and the cancellation never stops the
scoring that a Browser search or a filtered deck asks for.

**Why:** the scoring takes minutes on a large collection (229 seconds of a
230-second request on Andrew's 38,523 scorable cards). Before this, a Stats
window that the user closed kept its scoring running to the end and kept the
RWKV lock with it, so the main window waited for a page that was already
gone.

**Pinned by:** `test_cancelled_stats_scoring_stops_at_the_next_batch_and_frees_the_lock`,
`test_stats_scoring_without_a_cancel_generation_is_never_cancelled`,
`test_prepare_stats_retrievability_scores_stop_when_the_stats_window_closes`,
`test_cancelled_stats_scoring_drops_the_kept_score_memo`,
`test_stats_scoring_after_a_cancel_runs_to_the_end`
(`qt/tests/test_rwkv_scheduler.py`)

## ui.stats-total-knowledge

Given the Stats page in Advanced mode (the graph is Advanced-only,
`ui.retrievability-advanced-only`), the Total Knowledge graph shows, for each day from the first rating of a card in the page's
search through today, the sum of the cards' R that day under the
collection's algorithm only ("Known", blue; `sched.one-global-algorithm`,
`ui.stats-one-algorithm`), and it may show a second line, the cards of the
search rated at least once by that day ("Reviewed", grey, the upper bound).
It always covers the whole review history; the page's period does not apply.

It has a checkbox next to the legend, labelled "Reviewed", which
draws that line and is on when the graph loads, so Advanced mode looks as it
did before the checkbox. Under the graph it names the collection's algorithm
("Algorithm: FSRS-7", "Algorithm: RWKV-Curve" or "Algorithm: RWKV-Instant",
the names of the deck-options Algorithm list) and reads "Reviewed is an upper
bound on your knowledge: it counts every card you have ever rated, as if you
never forgot one."

The line under the title reads "The number of cards you would recall on each
day (the sum of their retrievability), over your whole review history.", the
word explained on hover.

Hovering a day shows the day's date, "Known: N cards" with N the day's sum
rounded to a whole number of cards, and, where the graph draws it,
"Reviewed: N cards". The rounding is for the tooltip only; the sum keeps
every decimal.

Hiding "Reviewed" changes the drawing only: the graph asks the backend for
the same data, the y axis keeps the bound's maximum as its top, the hover
tooltip drops the "Reviewed" row, and under RWKV the sweep line still moves
as the job reaches each day.

A mode switch does not compute the graph again. The Stats page keeps one
block per graph, keyed by the graph, so a graph that both modes show keeps
its block and everything in it over a switch: the loaded response, the RWKV
job and the days it has already computed, and the state of the "Reviewed"
checkbox. The graph asks the backend again only when the page's search
changes or the page is opened again.

A day is a scheduler day ("next day starts at" applies). A card's R on a
day comes from its last event on or before that day: a rating that day
gives 1; an earlier rating gives the algorithm's R at the whole days since
it; a reset (Forget) gives 0 until the card's next rating (the card stays in
the bound). Ratings are answers that affect scheduling: manual reschedules,
resets and cram answers are not ratings.

| Algorithm    | R after a rating                                                 | Drawn                    |
| ------------ | ---------------------------------------------------------------- | ------------------------ |
| FSRS-7       | FSRS-7's forgetting curve at the memory state after that rating  | at once                  |
| RWKV-Curve   | the curve RWKV stores at that rating, at the whole days since it | day by day, oldest first |
| RWKV-Instant | RWKV-Instant's prediction for the card on that day               | day by day, oldest first |

FSRS-7 replays each card's whole history with its preset's FSRS-7
parameters: every part of the history starts as the card's own memory state
does after a reset or a relearn, so the days before a reset follow that part's
replay. A rating FSRS-7 has no state for (a history without a learning step
before the first interday review) gives 0 on the days after it. The
replay runs in the backend off the main thread; while it runs the graph
reads "Calculating…".

Under RWKV the upper bound, where the mode draws it, shows at once. A
background RWKV job replays the
collection's review history day by day with a separate RWKV runtime and
sums the search's cards; the days it has not reached are blurred, and a
vertical line sweeps from left to right as it goes. RWKV's history of a card
starts at its latest learning start, as RWKV's scheduling does, so a card
reset and learned again has R 0 on the days before that start. A second
request for the same cards and collection state joins the running job; a
finished result is kept for the session, and the finished days across
restarts (`ui.stats-total-knowledge-incremental`); closing the Stats window or
leaving the page stops the job. With no usable RWKV model the graph reads
"RWKV model not found" (`sched.rwkv-no-model-error`).

**Why:** Andrew, 2026-09-15: port the Search Stats Extended add-on's
"Memorised" graph natively as "Total Knowledge", with only the active
algorithm, FSRS-7 with each card's preset parameters and a complete replay
from the first review, the full history whatever the period, and for the
slow RWKV a blurred bound with a sweep line; later the same day: shown in
Simple mode too. 2026-09-16: the bound is a power user's line, so Simple
mode shows the estimate alone with one sentence of plain English, and
Advanced mode gets the checkbox, the algorithm's name and the warning that
the bound assumes a perfect memory. Simple mode must not say
"retrievability", so it has its own subtitle. And a mode switch must not
throw away work the graph has already done, so the page keys its graph
blocks.

**Pinned by:** `fsrs7_sums_match_the_cards_historical_memory_states`,
`total_knowledge_covers_the_whole_history`,
`a_reset_zeroes_r_until_the_next_rating`,
`rwkv_collections_get_the_upper_bound_only`
(`rslib/src/stats/total_knowledge.rs`); FSRS-7's curve is the scalar copy
pinned by `scalar_curve_is_bit_identical_to_the_tensor_path`
(`rslib/src/scheduler/fsrs/curve.rs`);
`curve_day_sums_from_warm_up_are_the_stored_curves` (`rslib/src/rwkv/mod.rs`);
`qt/tests/test_total_knowledge.py`;
`ts/routes/graphs/total-knowledge.test.ts`;
"a mode switch keeps one block per graph that both modes show"
(`ts/routes/graphs/ui-mode.test.ts`);
"Total Knowledge: the modes differ, and switching does not load it again"
(`ts/tests/e2e/graphs.test.ts`).

## ui.stats-total-knowledge-incremental

Given the Total Knowledge graph under RWKV (`ui.stats-total-knowledge`), the
RWKV job keeps the sums of the days before today that it finished, per search
(the search's cards) and per algorithm, in the file
`collection.total-knowledge-cache.json` beside the collection, the eight
newest searches only. The next job for the same cards, in the same session or
after a restart, takes the kept sum for each of those days instead of scoring
the cards again. It still replays the whole review history day by day, so the
days after the kept ones start from the same RWKV state as before, and every
day's sum is the sum a full run gives, to the last bit. Today is never kept,
since its reviews are not all in yet.

The kept days are thrown away, and the job computes every day again, when
anything they read has changed: any review RWKV replays up to the last kept
day (its record, its day, so a moved day boundary too, and its target
retentions), a rating or reset of a searched card up to that day, the search's
cards, the RWKV model file, or the format version of the kept sums.

**Why:** Andrew, 2026-09-19: "Build stage 1 and keep cache across restarts".
The job reran from scratch on every Stats open after any change to the
collection: 34 min for his whole collection under RWKV-Instant on all
threads, 53 min under the background worker cap. RWKV is causal, so a day's
sum reads only the reviews up to that day, and the days before a new review
do not change.

**Pinned by:** `qt/tests/test_total_knowledge_cache.py`
(`test_a_run_that_reuses_the_days_matches_a_full_run_bit_for_bit`,
`test_a_changed_old_review_throws_the_kept_days_away`,
`test_the_kept_days_stop_before_today_and_survive_a_restart`).

## ui.plain-progress-text

Given RWKV's background work (reading the review history into the model at
start-up or on request, adding a sync's reviews, preparing the Stats graphs'
data, RWKV-Curve's reschedule), its progress windows, their results and the
two deck-options buttons that start the work speak in plain words, with no
"cache", "state", "calibration", "inputs" or "delta" and no elapsed time:

| Work                       | Window title    | Progress text                                                           |
| -------------------------- | --------------- | ----------------------------------------------------------------------- |
| Reading the review history | Getting Ready   | Reading your review history: 425,984 of 656,459 reviews, about 29s left |
| After a sync               | Getting Ready   | Adding the reviews from the sync...                                     |
| Stats graphs' data         | Preparing Stats | Preparing the Stats graphs                                              |
| RWKV-Curve reschedule      | Reschedule      | Calculating new due dates...                                            |

The deck-options buttons are "Read Review History Again" and "Prepare Stats
Graphs". Before the first review is done the text shows the counts without a
time. All the text is translatable.

The two waits that replace a screen's own content follow the same rule and
name no algorithm: the reviewer shows "Getting this card ready..." instead
of the answer buttons while the card's intervals are being calculated, and
the deck screen shows "Choosing your reviews..." while the deck's cards are
being scored.

**Why:** Andrew, 2026-09-19, about "Building RWKV state cache: 425,984/656,459
reviews | elapsed: 54s | remaining: 29s": "we need more user-friendly, less
jargon-y message for this" (asked before as well).

**Pinned by:** `test_reviewer_rwkv_warmup_progress_label_says_how_far_and_time_left`
and the updated progress assertions in `qt/tests/test_rwkv_scheduler.py`.

## ui.rwkv-algorithm-names

Given anything the user reads about RWKV's values (a graph series or legend,
a menu, a setting, a progress title, an error message), it names the
algorithm: "RWKV-Curve" or "RWKV-Instant", never a bare "RWKV". A bare "RWKV"
names only the model itself (its file, its state, its review history). So the
Stats Retrievability graph's series is named after the collection's
algorithm, the deck menu's reschedule submenu is "RWKV-Curve" (it reschedules
with RWKV-Curve), the deck-options box is named after the collection's RWKV
algorithm, "Keep RWKV-Curve intervals in answer order" and "Update the
RWKV-Instant queue" name the algorithm each setting belongs to, the new-card
orders by retrievability say "(RWKV-Instant)" (they read RWKV-Instant's
scores), the Browser's "calculating ... values" title names the algorithm its
search reads, and a filtered deck's failed preparation names the
collection's.

**Why:** Andrew, 2026-09-19: "make sure it says RWKV-Curve or RWKV-Instant,
not just RWKV. See if there are any other places with ambiguous naming."

**Pinned by:** `qt/tests/test_rwkv_algorithm_names.py` (a search and a
collection name their algorithm; no English label about values says a bare
"RWKV"), `ts/routes/graphs/retrievability.test.ts` ("the series is named after
the algorithm, not just RWKV").

## ui.stats-model-graph-drawing

Given the AUC-ROC, Calibration and Universal Metric+ graphs, each draws a
faint grid at its axes' ticks behind the data. The Calibration graph's count
bars each span their own bin, from its lower edge to its upper edge
(the bins narrow towards 1: bin i holds predictions from ln(i+1)/ln 21 to
ln(i+2)/ln 21), so no bar overlaps another and none sits away from its bin.
A model-quality or Total Knowledge graph that has data shows no "No data"
text over it; the overlay appears only with a message (calculating, an
error, or no data at all). The UM+ graph's title is "Universal Metric+
cross-comparison".

**Why:** Andrew, 2026-09-19: "NO DATA + some bars are overlapping while
others are far away from each other"; "add grid, plt.grid() kind"; "make
sure the title says Universal Metric+, not just UM+".

**Pinned by:** `ts/routes/graphs/calibration.test.ts` ("each count bar spans
its own bin, and the bins tile 0 to 1").

## ui.no-waiting-windows

Given the path a user takes from starting Clanki to answering a card
(start-up, the deck list, expanding or collapsing a deck, clicking a deck,
the overview, answering), Clanki opens no progress or waiting window, however
long an operation waits for something else. The operations of that path run
without one: expanding or collapsing a deck, clicking a deck, and answering a
card.

A window is shown only for the waits the user asked for and expects:
optimizing FSRS-7 parameters, creating a backup, checking the database,
checking the media, and operations of that kind (an import, an export, a
bulk edit the user started). The slowest Stats graphs say "Calculating..."
in their own place instead of opening a window.

**Why:** Andrew, 2026-09-20: "No 'Processing...'/'Getting review data
ready...'/'Preparing...'/'Starting...' neither at startup nor when a user
clicks on anything other than optimizing FSRS-7 parameters, creating a
backup, checking the database or media, or some other exception where it is
expected to wait... the whole path from Clanki startup to clicking on a deck
to reviewing a card should be seamless." A window that appears because
something else is busy tells the user nothing and takes the app away from
them.

**Pinned by:** `test_the_click_path_operations_open_no_waiting_window`
(`qt/tests/test_operations_no_waiting_window.py`).

## ui.close-says-what-it-waits-for

Given the user closes the main window, closing may wait twice, and neither
wait is silent.

**First, for background work.** The close is deferred while any `CollectionOp`
or `QueryOp` is still running, because a close-time collection read on the GUI
thread would block behind it. The close event itself is already ignored, so
until the wait ends the window simply stays. After one second of waiting,
Clanki opens a window titled "Closing Clanki", labelled "Finishing background
work before closing...", with a button "Keep Clanki open". The button
abandons the close and returns the user to Clanki: the running operation is
what the close waits for and nothing here can end it. Pressing Escape or the
title-bar X does the same. A close with nothing running finishes well inside
the second, so the usual close still opens no window
(`ui.no-waiting-windows`). Closing again starts the wait again.

**Then, for the sync.** The collection sync that runs on close (and the one a
user starts) opens its progress window titled "Syncing with AnkiWeb" and
labelled "Contacting AnkiWeb...", with a "Cancel" button. Before the server
answers there is nothing to report, and that label is what the window shows
for as long as the wait lasts; the sync replaces the title with its own stage
and the label with its added/removed counts as soon as it reports either.
Cancel aborts the sync, and closing then goes on: it is the sync that is
cancelled, not the close. Escape and the title-bar X do the same as Cancel,
as before.

A progress window shows a Cancel button only when it is asked for one. Escape
and the X have always set the cancel flag on every progress window; the
button makes that visible on the two waits above, and no other window gains
one.

**Why:** Andrew, 2026-09-21: "Clanki becomes unresponsive when I try to close
it", with a screenshot of a modal "Checking..." box and an indeterminate bar.
Told that the close waits for the sync, he answered that official Anki does
not freeze this way — correctly: upstream `closeEvent` calls
`unloadProfileAndExit()` at once, and the wait for background operations came
from the JSchoreels fork (`31a00619a`, `docs/collection-shutdown.MD`). That
trade was cheap in the fork and expensive in Clanki, which added RWKV
operations that run for minutes. Asked what closing should do, he chose "say
what it is doing and allow cancel". The old "Checking..." named neither the
step nor who was being waited for, and nothing on either wait said it could
be stopped at all.

**Pinned by:** `test_a_close_with_nothing_running_opens_no_window`,
`test_a_close_that_waits_says_what_it_waits_for`,
`test_cancelling_the_close_wait_keeps_the_app_open`,
`test_the_close_wait_window_closes_when_the_work_finishes`
(`qt/tests/test_main.py`);
`test_the_collection_sync_names_ankiweb_and_offers_cancel`
(`qt/tests/test_sync.py`);
`test_a_progress_window_has_no_cancel_button_unless_asked`,
`test_the_cancel_button_sets_the_same_flag_as_escape`
(`qt/tests/test_progress.py`).

## ui.close-off-main-thread

Given a close of the profile (quitting Clanki, switching profile, restoring a
backup), the collection's close-time work runs on a background thread, after
any collection task already running: the two-weekly optimize when it is due,
the integrity check, the backup (with its 5-minute throttle) and the close
itself, including the wait for the backup to finish. The main window stays
responsive, disabled, and a second close request in that time is ignored.
From the start of that work the main thread has no collection (`mw.col` is
None). A window says "Backing Up..." (or "Optimizing...", or "Closing..." when
restoring a backup) only when the work takes longer than 1 second. The
integrity check covers the collection only, not an attached cache: a cache
that fails the check does not make Clanki say the collection file looks
corrupt. A collection that fails it is closed without a backup and the
warning says so, as before.

**Why:** B-027, Andrew 2026-09-24: closing showed "(Not Responding)" for a
few seconds and the "Collection sync complete." tooltip drew black borders.
The check ran on the main thread over every attached database, the 937 MB
retrievability cache included: 5.5-5.9 s, against 0.47 s for the collection
alone. Andrew chose both steps: check the collection only, and move the
close work off the main thread. Measured offscreen on a copy of his profile,
3 runs each: the longest main-thread gap during a close went from 3.7-8.2 s
to 9.5-18 ms.

**Pinned by:** `test_the_collection_closes_on_a_background_thread`,
`test_a_slow_close_says_it_is_backing_up`,
`test_a_due_optimize_runs_with_the_close`,
`test_a_corrupt_collection_is_not_backed_up_and_says_so`
(`qt/tests/test_main.py`).

## ui.tooltip-style

Given a short message over the current window (a finished sync, a
reschedule, a copied value), Clanki draws one frameless widget: grey-blue
(#e2e5ec) with black text in the light theme, obsidian black (#0a0a0a) with
white text in the dark theme, rounded corners, a soft shadow, and no web
view. A click hides it, and it closes by itself
after its period. It is the same widget in both themes; the yellow panel
with a two-pixel frame is gone.

**Why:** Andrew, 2026-09-20: "sometimes Anki displays stuff in this window,
like for syncing or rescheduling. Can you make it look less 2004 and more
modern? As long as it doesn't bloat RAM and slow everything down"; the two
colour pairs are his ("grey-blue-ish with black text", "obsidian black
#0A0A0A with white text"). A QLabel
with a shadow costs no process and no page load, so a message stays as cheap
as it was.

**Pinned by:** `test_a_tooltip_is_a_rounded_toast_in_the_themes_colours`,
`test_a_tooltip_closes_on_a_click_and_on_close`
(`qt/tests/test_tooltip.py`).

## ui.stats-model-metrics

Given the Stats page in Advanced mode, the model-quality graphs compare the
scheduling algorithms on the same reviews. Simple mode never shows them.
These graphs are the one place where the values of two algorithms may stand
side by side (`sched.one-global-algorithm`, `ui.stats-one-algorithm`),
because comparing the algorithms is their whole purpose. Every drawn value
carries the name of the algorithm it comes from, no value ever falls back to
another algorithm, and an algorithm that cannot be computed is absent, with a
line under the graph that names it and says why.

They all use the same data: for every rating of the search's cards in the
page's period, the probability of recall an algorithm predicted before that
answer, and the answer itself (Hard, Good or Easy = remembered; Again =
forgotten). A rating counts only when it follows an earlier rating of the
same card in the same learning sequence, so a card's first rating, and its
first rating after a reset, are left out: no algorithm has a memory state
before them. Manual reschedules, resets and cram answers are not ratings.
The period selects the ratings; the algorithms still read the whole history
before each of them, because that is where the memory state comes from. A
card whose review log holds no learning step has no FSRS-7 prediction at
all, as in FSRS's own evaluation of its parameters, and RWKV's history of a
card starts at its latest learning start, as RWKV's scheduling does.

| Algorithm    | Its prediction of a rating                                                                                                              |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| FSRS-7       | FSRS-7's forgetting curve at the memory state after the card's previous rating, at the days since it, with the card's preset parameters |
| RWKV-Curve   | the recall of the curve RWKV stored at the card's previous answered review, at the time since it                                        |
| RWKV-Instant | RWKV-Instant's prediction of the card at that moment, from its state before the answer                                                  |

The predictions are not computed for the graph. Each algorithm writes them
per review while it runs, and the graph reads those rows: FSRS-7's when its
parameters are optimized, RWKV's when its state cache is built. RWKV-Curve's
value is recorded by the same replay that records RWKV-Instant's: the
warm-up already computes the curve head at every review, and its prediction
of a review is the curve the replay had stored at that card's PREVIOUS
answered review, at that review's own elapsed time. A card's first review
has no such curve and gets no row; nothing is substituted for it. RWKV-Curve's
value goes to RWKV-Curve's own recorder, and a replay that cannot report that
value does not run at all: the pass stops before it starts and says why,
rather than walking the whole history and recording nothing.

Each algorithm's rows live under its own name. RWKV-Curve's are in the
generic `review_predictions` table, which is keyed by algorithm as well as
by review, so a read cannot reach another algorithm's number without naming
whose it is; FSRS-7 and RWKV-Instant still have a table each, and moving
them is a separate change that must be bit-identical. Which sample roles
are legitimate, and whether the first honest role or the newest row of each
rating wins, are facts each algorithm declares rather than a rule the graph
applies to all of them.

An algorithm that could predict these reviews but whose rows nothing has
written yet is absent for THAT reason: the graph says Clanki has not
recorded its predictions yet. It never says the algorithm cannot compute
them, and it never tells the user to start the recording: the user never
has to decide to rebuild RWKV's history (CLAUDE.md, Planned direction 9). When an algorithm's rows begin part way through the
history, the graph says "recorded from &lt;date&gt;; N earlier reviews are not
recorded", so a series covering days cannot look like one covering years;
after a full replay records the history, that line is gone. A row counts
only when nothing that produced it was fitted on that very review:

| Algorithm | Rows that count                                                                 |
| --------- | ------------------------------------------------------------------------------- |
| FSRS-7    | a validation fold first, else a run after the optimization; never the final fit |
| RWKV      | any role, because the weights are frozen and were trained on other collections  |

FSRS-7 uses one role only, the first role of its list that has any row,
because that list is in order of honesty, and the graph names it. RWKV's
roles carry no such order, so RWKV reads all of them and takes, of each
rating, the row written last, whatever its role: the newest row is the
current model's, and a bigger but older recording no longer wins. RWKV
names no role. One rating gets one number: of several rows the graph takes
the one written last, and of two written at the same moment the higher
fold, and of two of the same fold the one whose source name sorts first. A
recording that runs again therefore replaces what the earlier one left,
instead of the two of them deciding the graph by chance.

The first rating of each card is never scored, for any algorithm and in
every graph, and srs-benchmark leaves that rating out too. "First" is over
the card's whole history, whatever the period. Before its first answer, RWKV
knows only a card's deck, preset and creation date: it was not trained to
predict recall for first reviews, and without the card's content those three
cannot tell apart the cards of one deck. The tooltips do not explain this
exclusion (Andrew, 2026-09-24).

When two or more algorithms have usable rows for the search and period,
every graph scores all of them on the same ratings: the ones every such
algorithm has a row for. Each algorithm on its own ratings would compare
different reviews. On Andrew's collection the ratings only RWKV-Instant had
(42,610 of its 80,329 extra ratings were first reviews) put its AUC at 0.705,
below RWKV-Curve's 0.714, while on the 426,703 ratings both had it was
0.740 against 0.727. An algorithm alone keeps every rating it has a row
for. Under the graph, the page gives the number of ratings scored ("Scored
on N reviews that every algorithm predicted." with two or more algorithms),
then the ratings no algorithm scored. A rating with no usable row for an
algorithm is never filled in from another algorithm's value, and never from
a fresh computation with today's parameters, because those parameters have
seen the rating.

Reading the rows does not start any computation. Ratings newer than the
newest stored prediction are left out, and the graph says "Predictions up to
&lt;date&gt;; N newer reviews are not scored yet", so it never silently drops
the newest reviews.

The calibration graph draws one algorithm, picked from a menu of all three:
an algorithm that cannot be scored is in the menu but cannot be chosen, and
is never replaced silently. The menu keeps its choice while the page reloads
its data, over a Simple / Advanced switch and over a change of period, and
choosing another algorithm computes nothing again, because the data is the
same for every algorithm. The drawing area is square. The x axis is the
predicted probability of recall in 20 bins, spaced so that the crowded high
probabilities get more bins than the low ones; the y axis is the share of
each bin's ratings that were remembered. A dashed diagonal is a perfect
algorithm: a point above it means the algorithm predicted too little, below
it too much. Each point carries a vertical line through the 2.5 and 97.5
percentiles of its share, from 500 resamples of the CARDS (not of the
ratings, because a card's own ratings are not independent). The same
reviews always give the same line: the cards are resampled in a fixed
order, from a fixed seed, so opening the page twice does not move the error
bars. The ratings of
each bin are drawn as blue bars behind the line (`#3f84cc` at opacity 0.45),
on their own axis at the right. The y axis is "Actual retention" and the x
axis "Predicted retention". Three tiles above the graph give the average
predicted probability, the actual retention, and the number of ratings.

The UM+ comparison draws a PAIR of algorithms, picked from a menu of the
pairs that share ratings. Three algorithms make three pairs. A pair is
offered only when at least 200 ratings have a prediction from both of its
algorithms; a pair with fewer is not in the menu and is not drawn, and is
named under the graph with the ratings it has, the ratings it needs, and
what would fill it. UM+ spreads a pair's ratings over 41 groups, so under
200 the middle groups hold a handful of ratings each and one card's run of
answers moves a bubble visibly: such a graph misleads worse than an absent
one. Its shape is wide, not square. Each point is a
group of ratings whose two predictions differ by about the same amount: the
x axis is that difference (the first algorithm's prediction minus the
second's, in 41 groups, one for each twentieth from -1 to 1), and the height
of a point is that algorithm's mean prediction minus the mean answer in the
group. A bubble's area is the group's share of the ratings. A switch adds
the groups of fewer than 200 ratings, which are hidden by default; the graph
says how many it hides. The legend gives each algorithm its UM+ - the root
mean square of its groups' mean errors, weighted by the groups' sizes, where
closer to zero is better - and the slope of its errors against the
difference, weighted the same way. The binning and the weighting are
`UM_plus_plot.py`'s, so the numbers can be compared with the ones from the
benchmark.

Under the UM+ and AUC-ROC graphs, one line says which way is better: "UM+
and slope: closer to 0 is better. Against the perfect oracle, any imperfect
algorithm has a slope of 1." and "AUC: higher is better, and 0.5 is random
chance." The calibration graph has no line under it. How to read a graph and
how its numbers are made (the axes, the bubbles, the oracle, the bins and
bars, the true and false positive rates, and which answers count as
remembered) is in a tooltip on the info badge next to the graph's title,
shown on hover or keyboard focus. The lines about the data itself (hidden
groups, the ratings each algorithm scored, missing or newer predictions)
follow the explanation in that tooltip for UM+ and calibration, and stay
under the graph for AUC-ROC. Andrew, 2026-09-23: "Reading is for nerds, lol.
Let's not have too much text unless the user asks for it", and "Just keep
some simple 'lower=better' or 'closer to 0=better' stuff outside of the
tooltip"; 2026-09-24: move the long text under the UM+ and calibration graphs
into the tooltip, rename "Actual recall" to "Actual retention" and "Predicted
retrievability" to "Predicted retention" (the graph keeps its name), and make
the blue bins less transparent and more saturated.

The AUC-ROC graph draws one curve per algorithm, all at once, with no
chooser. A curve plots the true positive rate against the false positive
rate at every prediction threshold. The drawing area is square, so the
dashed line of random chance runs at 45 degrees; that line's legend entry
reads "Random chance, AUC=0.5000". Each algorithm's legend entry is its name
and its area under the curve to four decimals, such as "FSRS-7,
AUC=0.7230". Ratings with the same prediction form one step of the curve,
and the area follows the trapezoid rule. An algorithm whose reviews were all
remembered, or all forgotten, has no curve. Each curve uses the ratings its
own algorithm predicts, so a curve appears as soon as its data is ready and
does not wait for the others.

The reading runs in a background job, so the page never waits for it and
reads "Calculating…" meanwhile. A second request for the same cards, period
and collection state joins the running job, a finished result is kept for
the session, and a Simple/Advanced switch or a second visit does not start
it again. Leaving the page or closing the window stops the job.

Andrew, 2026-09-16: the graphs first restricted every algorithm to the
ratings all of them shared. On his collection that rule hid every curve.
RWKV had rows for 9154 of the 9155 ratings of his current deck and FSRS-7
had four, so three ratings survived the intersection, all three were
answered Again, and an algorithm whose ratings were all forgotten has no
curve. Comparability is a property of a number, not a reason to throw data
away, so the page now names the shared count instead of enforcing it.

**Why:** Andrew, 2026-09-16: add the Search Stats Extended fork's
model-quality graphs, so that the algorithms can be compared on his own
reviews. The no-mixing rule is deliberately relaxed here and nowhere else,
because a comparison of one algorithm with itself says nothing; the honesty
rules (a name on every series, no fallback, an absent series with a reason)
are what keep the relaxation safe. All three curves at once on the AUC-ROC
graph, and the square drawing area with the labelled diagonal, are his
words, as is the UM+ comparison of a pair, which he asked for from the
Search Stats Extended fork together with the `UM_plus_plot.py` binning of
the srs-benchmark. The rules about which stored rows count, and about
scoring both algorithms on the same ratings, are the RWKV session's: a prediction from a
model fitted on the very review it predicts flatters that model, and two
scores over two different sets of reviews cannot be compared. Andrew,
2026-09-23, after the graph put RWKV-Instant below RWKV-Curve ("RWKV-Instant
not having higher AUC than Curve is sus"): score them on the shared
ratings, leave each card's first rating out, and read RWKV's newest row of
each rating; the RWKV session agreed, and on the benchmark Instant beats
Curve for 99.5% of users. Reading the
stored rows rather than replaying is what the Search Stats Extended fork
does, and it is why a panel of hundreds of thousands of reviews opens at
once; a replay of the whole history costs minutes and now belongs to the
user's own rebuild, never to opening a page.

**Pinned by:** "UM+ keeps its verdict under the graph and its explanation in
the tooltip", "AUC-ROC keeps its verdict under the graph and its explanation in
the tooltip", "calibration puts its whole explanation in the tooltip", "the
notes about the data follow the explanation in the tooltip"
(`ts/routes/graphs/metric-explanations.test.ts`),
`test_the_metric_graphs_say_which_way_is_better_in_one_line`
(`qt/tests/test_ui_split.py`); `only_rows_the_algorithm_had_not_seen_are_used`,
`a_single_rating_of_one_algorithm_narrows_the_comparison_to_it`,
`two_algorithms_are_scored_on_the_ratings_both_scored`,
`one_algorithm_alone_keeps_all_of_its_ratings`,
`rwkv_takes_the_newest_row_of_each_rating_across_its_roles`,
`a_cards_first_rating_is_never_scored`,
`the_period_selects_the_ratings`,
`newer_ratings_than_the_stored_predictions_are_reported`,
`calibration_bins_and_their_intervals`,
`um_plus_groups_the_ratings_by_how_far_the_algorithms_differ`,
`curve_values_survive_a_card_split_across_two_warm_up_calls`,
`bulk_warm_up_curve_values_match_sequential_over_a_batch`
(`rslib/src/rwkv/mod.rs`),
`the_same_reviews_always_give_the_same_interval`,
`the_parallel_bootstrap_draws_what_one_thread_drew`,
`the_ratings_read_are_the_searched_cards_ratings`
(`rslib/src/stats/review_metrics.rs`),
`the_newest_row_of_each_review_is_the_one_read`
(`rslib/src/storage/revlog/mod.rs`);
`test_rwkv_calibration_recompute_records_the_curve_of_every_review`,
`test_the_state_cache_build_records_rwkv_curve_rows_too`,
`test_rwkv_calibration_recompute_refuses_a_backend_that_cannot_record_the_curve`,
`test_bulk_warm_up_is_handed_the_curve_recorder`,
`test_bulk_warm_up_without_a_curve_recorder_keyword_says_so`,
`test_a_query_with_no_prediction_is_skipped_not_reported`
(`qt/tests/test_rwkv_scheduler.py`); `qt/tests/test_stats_metrics.py`;
`ts/routes/graphs/roc.test.ts`; `ts/routes/graphs/calibration.test.ts`;
`ts/routes/graphs/um-plus.test.ts`.

## ui.stats-graph-colours

Given the Stats graphs, their colours and their axis steps are these:

| Graph                   | What it draws            | Colour              |
| ----------------------- | ------------------------ | ------------------- |
| Card Retrievability     | the FSRS-7 series        | green `#2f9e44`     |
| Card Retrievability     | the RWKV series          | blue `#1c7ed6`      |
| Calibration             | the count bars behind it | blue `#6ba3d6`      |
| Calibration             | the perfect diagonal     | grey `#8a8a8a`      |
| AUC-ROC                 | the random-chance line   | grey `#8a8a8a`      |
| AUC-ROC and Calibration | one curve per algorithm  | `ALGORITHM_COLOURS` |

The AUC-ROC and Calibration graphs step both axes by 0.1, from 0 to 1, on
eleven ticks each. Only the two reference lines are grey: a grey series
reads as a graph that is turned off.

**Why:** Andrew, 2026-09-16: the amber Retrievability bars and the grey
calibration count bars "look lame"; he asked for blue or green, and for
0.1 steps rather than 0.2 on both graphs.

**Pinned by:** "the two retrievability series draw green and blue, and
neither is amber" (`ts/routes/graphs/retrievability.test.ts`); "both axes
step by 0.1" (`ts/routes/graphs/roc.test.ts`); "both axes step by 0.1, and
the count bars are blue" (`ts/routes/graphs/calibration.test.ts`).

## ui.stats-fsrs-predictions-ready

Given a collection with FSRS-7 presets, Clanki keeps FSRS-7's per-review
predictions stored and ready, so the model-quality graphs find rows instead
of asking the user to make them. The user starts nothing and presses
nothing.

The pass that writes the rows runs off the main thread, and the Stats page
never waits for it. It runs after the collection has opened, at most once a
day, and again whenever a preset's FSRS-7 parameters change.

It waits for a pause in what the user does **before it begins**. It asks
which presets are stale only once ten seconds have passed without a key
press, a click, a double click, a scroll or a touch anywhere in Clanki. That
question holds the collection, and so does every preset after it; at
start-up, when the pass began at once, that froze the main window for 2-4
seconds and made deck options take 3.5 seconds to open. While it waits for
that pause it has written nothing. When the collection closes during the
wait, the pass stops **at once** — it looks again four times a second, not
at the end of the countdown it is in — without counting the day as done and
without reporting a failure.

**Once it has begun it never waits for the user again.** Between two presets
it rests instead, and that rest is bounded. While the user works it is twice
the preset just done, up to five seconds. While the user is away, or where
nothing tracks him, it is one twentieth of a second, which is only long
enough to let a click that is already waiting for the collection take it
first. The same rest separates two automatic optimizations. The pass holds
nothing across a rest: it takes the collection inside a backend call and
gives it back when that call returns, so there is no lock to hand back.

**A click waits for one batch of rows, not for a preset.** A preset's rows
go in in batches of at most ten thousand, each batch its own write, and the
collection is free between two of them; the pass rests a moment there, long
enough for a click that is already waiting to take the collection first.
This is the shape the RWKV recording pass uses
(`sched.rwkv-recordings-automatic`).

Every batch checks again that the preset is still the one the job read: its
saved time, its FSRS-7 parameters and the decks that use it. If any of them
differs, the preset was saved while its rows were being written. The pass
then deletes the batches it has already written for that preset, writes no
more, and reports nothing written; the preset stays stale for the next pass.
Only the rows this pass wrote go, named by review id, so another preset's
rows are untouched. The cache therefore holds no mixture of rows from two
sets of parameters: a parameter change deletes that preset's stored rows in
the same transaction as the change, and whatever the pass wrote after that
deletion it takes back itself.

A pass cut off part way through a preset — the profile closes, Clanki stops
— leaves the batches it had written. They are validation folds made by the
parameters in force, so they are kept: the next pass finds the preset stale,
because the reviews it never reached have no fold, and writes it again.

It never starts while the RWKV state cache is loading or building. That load
holds the collection, so a pass in front of it would make the user wait for
a backfill before a restore that is already slow; the pass asks again every
few seconds instead, and starts once the load is done. It recomputes ONE
PRESET PER CALL, with the collection free between presets and free between
the batches inside one of them, so the main thread waits at most for one
batch rather than for a whole backfill. It
reports no progress of its own and clears none, so it cannot wipe or fight
the progress the main thread is showing; while it runs, the user sees
nothing except the graphs' own "its predictions for these reviews are being
computed". It
covers every preset that has a rated review no stored validation fold
covers, over that preset's whole card set and the whole collection: it never
follows the search or the period the Stats page happens to show, because a
pass that filled only the deck on screen would leave every other deck
without rows.

When a preset's FSRS-7 parameters change, every prediction those parameters
produced is wrong, and Clanki deletes that preset's stored rows in the same
transaction as the change. Only that preset's rows go; parameters are per
preset, and another preset's rows were made by parameters that did not
change. Desired retention, easy days and fuzz change the schedule but not
the prediction, so they delete nothing.

Between the deletion and the end of the pass, FSRS-7 has no rows. The
graphs then show FSRS-7 as absent with its own reason, "its predictions for
these reviews are being computed", and never draw a value that the current
parameters did not produce. No other algorithm is drawn in its place.

A pass that fails says so: Clanki shows one message in that session naming
what could not be stored, and does not count that day as done, so the pass
tries again. An empty FSRS-7 series on its own cannot be told apart from a
series still being computed, and a pass that reports no progress reports no
failure either.

The stored rows are validation folds, so nothing that produced a row had
seen the review it predicts. The rows written while answering carry a
different sample role, and the graph takes the first role of its list that
has any row, so those rows stay hidden behind the folds and answering does
not keep the set fresh; the pass rerunning is what keeps it fresh. Reviews
newer than the newest stored prediction are named by the graph's own
staleness line, never silently dropped.

**Why:** Andrew, 2026-09-16: "now we need to make FSRS-7 always have
predictions ready", and, on what a parameter change means, "If FSRS
parameters change, then all predictions must be recalculated. This is true
both for using FSRS in practice and for Stats." A button the user must find
is not "ready", so the pass runs by itself. Recalculating rather than
labelling each row with the parameters that made it is his choice: a
prediction from superseded parameters is not a weaker prediction, it is the
wrong number. The pass costs about 25 seconds on a collection of
910,715 rated reviews, measured on main after the parallel replay of pull
request 125, which is why it runs in the background and at most once a day
rather than while a page is open.

The rest between presets is the same rule seen from the other side. The pass
used to wait for the same ten seconds of quiet before every preset, and a
user who keeps working never gives it one, so it made no progress at all
while he studied: measured on his collection, headless with a simulated user
acting every three seconds, 0 of 11 presets and 0 rows in five minutes, for
a job of 25 seconds. With the bounded rest the same pass wrote all 966,822
rows in 52 seconds of the same use, for 25 CPU seconds, and 93 per cent of
the clicks sampled during it waited less than a twentieth of a second. With
the user away both take the same time, 25 seconds. Looking for the closed
profile four times a second rather than once a countdown is the same fault
again: the pass used to stay alive for nine seconds after the profile closed
under it, and now stops within a quarter of a second.

The batched write is the same rule once more. One write per preset held the
collection for as long as that preset took: on Andrew's collection, headless
with a simulated user acting every three seconds, the largest preset
(339,239 rows) made a click wait 6.0 seconds, and about 15 of the 48 seconds
of the run were spent holding the collection. With the write in batches the
worst click of a whole pass is under half a second, its median under a
tenth, and the collection is held for under two seconds of the run. The
check per batch costs one read of the preset and of the deck list per batch,
which is a few milliseconds beside the write it guards.

**Pinned by:** `test_the_pass_waits_for_the_rwkv_state_cache`,
`test_the_collection_is_free_between_presets`,
`test_a_pass_that_fails_says_so`,
`test_the_pass_waits_for_a_pause_in_what_the_user_does`,
`test_a_started_pass_never_waits_for_the_user_to_stop`,
`test_the_rest_between_presets_is_a_bounded_multiple_of_the_preset`,
`test_a_pass_waiting_for_a_pause_stops_when_the_collection_closes`,
`test_the_fake_backend_returns_what_the_real_backend_returns`
(`qt/tests/test_fsrs_predictions.py`);
`a_parameter_change_drops_that_presets_predictions`,
`another_presets_predictions_survive_a_parameter_change`
(`rslib/src/deckconfig/update.rs`);
`the_pass_covers_every_preset_with_uncovered_reviews`,
`a_presets_rows_are_written_one_bounded_batch_at_a_time`,
`a_save_midway_through_the_write_takes_back_what_was_written`
(`rslib/src/scheduler/fsrs/predictions.rs`);
`qt/tests/test_fsrs_predictions.py`; `ts/routes/graphs/roc.test.ts`.

## ui.browser-memory-columns

Given the Browser in cards mode, its Retrievability, Stability and
Difficulty columns show the collection's own algorithm's values only
(`sched.one-global-algorithm`):

| Algorithm    | Retrievability                                     | Stability          | Difficulty |
| ------------ | -------------------------------------------------- | ------------------ | ---------- |
| FSRS-7       | FSRS-7's R now                                     | FSRS-7's stability | FSRS-7's   |
| RWKV-Curve   | the stored curve at the time since the last review | that curve's S90   | blank      |
| RWKV-Instant | RWKV-Instant's R for the card now                  | blank              | blank      |

A card keeps its FSRS-7 memory state under RWKV, and it is never shown
there. The RWKV values are the ones card info shows
(`ui.rwkv-curve-r-stored-curve`, `ui.card-info-one-algorithm`). They are
computed in the background for the rows the table draws, a batch at a time,
so scrolling never waits for RWKV; a cell is blank until its value arrives,
and a card RWKV has no value for (a new card, no stored curve, no model)
stays blank, with no other algorithm's value in its place. A value is
computed again when its row is drawn after it went stale
(`sched.rwkv-r-freshness`); until the new one arrives the cell keeps the
old text. In notes mode the three columns stay blank under RWKV.

Sorting by Retrievability under RWKV sorts by the algorithm's R, which the
Browser prepares for the search before it runs it, as for a `prop:rwkv…`
search (`ui.browser-rwkv-search-does-not-block`); cards without a value come
first in ascending order. Stability and Difficulty cannot be sorted by under
RWKV.

**Why:** Andrew, 2026-09-19: "All active cards should have retrievability
values, regardless of which algorithm is being used, including using
RWKV-Instant", and "Stability should be shown for FSRS-7 and RWKV-Curve,
difficulty only for FSRS-7". Before, the columns showed FSRS-7's values
under RWKV, which mixes algorithms.

**Pinned by:** `memory_columns_show_fsrs7_values_under_fsrs7_only`
(`rslib/src/browser_table.rs`),
`rwkv_sort_by_retrievability_reads_the_algorithms_published_r`
(`rslib/src/search/mod.rs`); `qt/tests/test_browser_memory_columns.py`;
`test_sorting_by_retrievability_under_rwkv_prepares_the_algorithms_scores`
(`qt/tests/test_browser.py`).

## ui.browser-rwkv-search-does-not-block

Given a Browser search that asks for RWKV values (`prop:rwkv:r…` or
`prop:rwkv-curve:r…`), Clanki prepares the scores of that search before it
runs the search, because the rows depend on them. While it prepares them:

- the window stays usable. There is no progress window over the Browser, the
  table keeps the rows of the search before it, and the editor keeps the note
  of the selected row;
- the window title says that Clanki is calculating RWKV values. The title
  returns to the normal "Browse (n of m cards selected)" when the rows of the
  new search arrive;
- while RWKV is still loading its state, the preparation reports that at once
  instead of holding the collection until the state is there. The Browser asks
  again every 250 ms, with the collection free in between, for at most 120
  seconds. After that the search runs with the scores RWKV has;
- a new search, or the Browser closing, ends the wait of the search before it.

FSRS-7 values never stand in for RWKV's: the rows of a search that asks for
RWKV values appear only when RWKV has given them.

**Why:** Andrew, 2026-09-16: the first Browser open waited up to two minutes
before the editor showed the note. The search held the collection for the
whole RWKV warm-up (`_RWKV_STATS_WARMUP_WAIT_TIMEOUT_SECS`, 120 seconds)
behind a progress window, so nothing in the Browser could move, not even the
parts that need no RWKV value.

**Pinned by:** `test_rwkv_browser_search_prepares_scores_before_searching`,
`test_rwkv_browser_search_waits_without_blocking_the_window`,
`test_rwkv_browser_search_stops_waiting_after_the_time_limit`,
`test_non_rwkv_browser_search_runs_without_preparation`
(`qt/tests/test_browser.py`)

## ui.browser-interval-average

Given a Browser row with review or relearning cards, the Interval column
shows the average of their intervals: in cards mode the card's own
interval, in notes mode the mean over the note's review and relearning
cards (new and learning cards do not count; no such card = an empty cell).
The average is exact for any intervals, also when they add up to more than
49,710 days.

**Why:** the FSRS-7 audit, 2026-09-15 (Andrew: fix it): the average was
computed in 32-bit seconds, which overflow past 49,710 days, so a note with
two 30,000-day cards showed about 5,144 days (a debug build crashed).

**Pinned by:** `interval_cell_averages_long_intervals_without_overflow`
(`rslib/src/browser_table.rs`).

## ui.background-change-dim

When an operation changes the collection while the Clanki window is in the
background, the screen behind it dims to 30% opacity. When the window receives
focus again, the screen returns to full opacity, on the deck list, the overview
and the reviewer alike.
**Why:** the dim marks a screen whose numbers may be stale. Before this entry
only the reviewer restored it, so the deck list stayed dim until an unrelated
redraw (Andrew, 2026-09-16: "Sometimes after loading RWKV cache on app start I
get this weird bug where the main menu is kinda grayed out ... going back to
main menu after that fixes it").
**Pinned by:** `test_focus_undims_the_deck_list_and_the_overview`
(`qt/tests/test_main.py`)

## ui.deck-list-refresh-scroll

Given the deck list on screen and a refresh of it that only reads the
counts again — the 10-minute timer, the end of a sync, a return of the
focus to the main window, an answer or an operation of another screen that
changed the study queues, a deck-options or preset change, a deck added,
renamed, moved or deleted, and any add-on that calls
`mw.deckBrowser.refresh()` — Clanki keeps the scroll position of the page.
The counts, the review limits and the heatmap show the new numbers. Where
the page can stay (the stats section under the tree, the heatmap included,
does not change and no add-on decorates a freshly loaded page), only the
rows of the deck table are swapped, as on a collapse; otherwise the page is
drawn again and the position is put back around the draw. Given a collapse
or an expand made while such a refresh reads the counts, the deck keeps the
state the user chose, so the rows under the kept position are the same
rows.

Given the user opens the deck list from another screen (start-up, the end
of a review, Decks in the toolbar), the page starts at the top.

**Why:** Andrew, 2026-09-16: the deck list jumped back to the top every 10
minutes, because the timer drew the page again without the position. A
refresh that only changes numbers must not move the view; opening the
screen is not a refresh.

**Pinned by:** `test_refresh_keeps_the_scroll_position_of_the_open_page`,
`test_refresh_swaps_the_deck_table_in_place_when_the_page_can_stay`,
`test_show_draws_the_deck_list_at_the_top`,
`test_refresh_draws_from_the_top_on_another_screen`,
`test_a_collapse_during_a_refresh_survives_the_refresh`
(`qt/tests/test_deckbrowser.py`).

## ui.answer-button-focus-visible

Given an answer button in the reviewer (Again/Hard/Good/Easy), its dashed
focus-indicator border shows only when the button was reached by keyboard
navigation (for example Tab), and never after a mouse click, whether the
mouse button is still held down or has already been released. The same rule
applies to the bottom bar's generic focus indicator (its border colour),
which is not specific to the answer buttons.

**Why:** Andrew, 2026-09-17: "if I click and hold LMB on the answer button,
the borders become dashed, but if I release it afterwards, they stay
dashed". A mouse click also focuses the clicked element, so a plain
`:focus` rule cannot tell a mouse click from a keyboard tab; `:focus-visible`
can, and the dashed border is meant to help keyboard users find the
focused button, not to react to a mouse click. The `.answerIncorrect:focus`
/ `.answerCorrect:focus` border-COLOR rules are left as plain `:focus`: they
set the same colour that the button already has at rest and on hover, so
they draw no distinct focus indicator and cannot exhibit this bug.

**Pinned by:** `answer button focus indicator shows for keyboard focus, not
for a mouse click` (`ts/tests/e2e/reviewer-focus-visible.spec.ts`).
