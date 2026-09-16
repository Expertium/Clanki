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
stays under File); these buttons share one width in both modes, the
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
Reviews, Card Counts, Retention and Total Knowledge graphs, in their usual
order; Advanced mode shows every graph. The page takes the mode when it
loads and from its own switch. Hidden settings keep their stored values and
keep taking effect.

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
installed).

**Pinned by:** `qt/tests/test_ui_mode.py` (toggle markup, click handling,
deck-browser row, the RWKV submenu, the switch redrawing without a full
reset), `qt/tests/test_advance_postpone.py` (the Advance and Postpone
entries),
`advanced_ui_flag_is_reported` (`rslib/src/deckconfig/update.rs`);
`test_deck_options_mode_switch_sets_the_main_window_mode`
(`qt/tests/test_ui_mode.py`); "the deck-options switch changes the view at
once" (`ts/tests/e2e/deck-options.test.ts`); `graphs_report_the_ui_mode`
(`rslib/src/stats/graphs/mod.rs`); "Simple mode keeps only the Simple
graphs, in page order" (`ts/routes/graphs/ui-mode.test.ts`).

## ui.simple-recall-wording

Given Simple mode (`ui.mode-switch`), no text the user sees calls the chance
of recalling a card now "retrievability"; it is called "Probability of
recall" instead. Advanced mode keeps the technical word. The places that show
the word in both modes are:

| Where                                         | Simple mode                                                                                    | Advanced mode                                                                           |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Browser column (name and notes tooltip)       | Probability of recall                                                                          | Retrievability                                                                          |
| Card info, the forgetting-curve tooltip       | Probability of recall                                                                          | Retrievability                                                                          |
| Filtered deck, the "Cards selected by" orders | Ascending / Descending probability of recall                                                   | Ascending / Descending retrievability                                                   |
| Filtered-deck rebuild failure (RWKV)          | RWKV probability of recall scores could not be prepared, so the filtered deck was not rebuilt. | RWKV retrievability scores could not be prepared, so the filtered deck was not rebuilt. |

The Browser reads the mode when it builds its column list and the
filtered-deck dialog when it opens, so a mode switch reaches those names the
next time the window is opened. Only the text
changes: the search syntax (`prop:r`), the column key `retrievability`, the
order of the cards, and every API and protobuf name stay as they are. The graphs,
deck-options settings and dialogs that name retrievability show in Advanced
mode only (`ui.mode-switch`, `ui.advance-postpone`,
`deck-options.simple-view`), so they keep the technical word everywhere.

**Why:** Andrew, 2026-09-16: "don't use the word 'retrievability' in Simple
mode"; he chose the replacement wording "probability of recall". Simple mode
is for users who do not read the FSRS papers. "Probability of recall" states
what the number is; "memory strength" would be wrong, because that is
stability.

**Pinned by:** `simple_mode_names_the_retrievability_column_in_plain_words`
(`rslib/src/browser_table.rs`);
`simple_mode_names_the_filtered_deck_orders_in_plain_words`
(`rslib/src/decks/service.rs`); "Simple mode's forgetting-curve tooltip does
not say retrievability" and "Advanced mode's forgetting-curve tooltip keeps
retrievability" (`ts/routes/card-info/forgetting-curve.test.ts`);
`test_filtered_deck_failure_avoids_retrievability_in_simple_mode`
(`qt/tests/test_ui_mode.py`).

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
90.0% → 93.5%"). OK (disabled at 0) moves the first that many cards in the
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

## ui.review-heatmap

Given the collection flag `reviewHeatmapEnabled` on (the default), Clanki
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
except a color scheme left at the add-on's own default. The heatmap's gear
opens the tab; Shift+click on the gear cycles the color scheme and
Shift+click on "today" cycles the calendar mode. With the switch off, or
where neither the calendar nor the figures show, nothing is computed.

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
enable the add-on must be told that Clanki has this built in.

**Pinned by:** `qt/tests/test_review_heatmap.py` (streaks, averages, the
day map, settings parsing and defaults, the carry-over from the add-on,
colors, modes and visibility, the stats-screen period and scope, the
render cache, the browser search, the Shift+click cycling, the settings
link, disabling the add-on, the one-time notice,
`test_enabling_the_review_heatmap_addon_is_refused_with_a_message`,
`test_installing_the_review_heatmap_addon_leaves_it_disabled`);
`test_update_collection_writes_the_review_heatmap_preference`
(`qt/tests/test_preferences.py`);
`review_heatmap_is_on_by_default_and_a_reviewing_preference`
(`rslib/src/preferences.rs`).

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

## ui.card-info-rwkv-curve

Given a card whose preset runs RWKV-Curve, card info's forgetting-curve chart
shows only RWKV-Curve's own curve: the curve RWKV stored for the card at its
last answered review, from that review to now and then as a dashed preview.
It draws no FSRS-7 segments for the reviews before it, since RWKV's past
curves are not stored. The chart's tooltip, card info's "Stability" row and
the latest review's stability in the page data show that curve's S90 (where
it meets 90% recall), not the S90 stored on the card. While RWKV has no
curve for the card (its state still loading, busy, or no answered review),
the chart shows no data and card info has no "Stability" row. The curve
reaches the page as recall at 0 and at 300 elapsed times evenly spaced in
log time from one minute to 100 years, joined by straight lines. The chart
starts at the card's latest answered review, whether or not FSRS-7 has a
memory state for the card (`ui.card-info-curve-messages`).

Card info for such a card shows no other FSRS-7 value either: no
"Difficulty" row, and its one "Retrievability" row is the curve's recall
now (`ui.card-info-one-algorithm`). The page data of the reviews before the latest answered one carries no
memory state, so no FSRS-7 stability of those reviews reaches the page.
Cards of FSRS-7 presets draw FSRS-7's curve with its S90 for every review;
RWKV-Instant cards draw none (`ui.card-info-one-algorithm`). With no curve the
box says why (`ui.card-info-curve-messages`).

**Why:** Andrew, 2026-09-15: forgetting curve graphs always use the S90, for
RWKV-Curve as for FSRS-7; the Stability row and the tooltip show the drawn
curve's S90; with no RWKV curve, hide the segment; never mix two algorithms
in one display, in general (so no FSRS-7 segments or values in an
RWKV-Curve card's info); and the older reviews of an RWKV-Curve card show
no FSRS-7 S90.

**Pinned by:** `card_curve_points_are_the_curve_and_its_s90`
(`rslib/src/rwkv/mod.rs`);
`test_rwkv_card_info_curve_samples_the_stored_curve`,
`test_rwkv_card_info_curve_is_none_without_a_curve`,
(`qt/tests/test_rwkv_scheduler.py`);
`test_card_info_gets_rwkv_curves_own_curve_and_s90`,
`test_card_info_has_no_rwkv_curve_for_other_algorithms`
(`qt/tests/test_mediasrv.py`); "an RWKV-Curve card shows its curve's S90
and R, and no difficulty", "an RWKV-Curve card without a curve shows no
stability and a calculating R" (`ts/routes/card-info/lib.test.ts`);
"rwkvRecallAt interpolates between the
curve's points", "an RWKV-Curve card's chart starts at its last review: no
FSRS-7 segments", "after the last review an RWKV-Curve card follows RWKV's
curve and S90", "without an RWKV curve yet the chart stops at the last
review" (`ts/routes/card-info/forgetting-curve.test.ts`).

## ui.card-info-curve-messages

Given card info's "Forgetting Curve" box with no curve to draw, it shows a
short message that says why, never the words "NO DATA", and never a value of
the other algorithm (`sched.one-global-algorithm`):

| Case                                                   | Message                                                                     |
| ------------------------------------------------------ | --------------------------------------------------------------------------- |
| RWKV is not ready (state loading, or another thread holds it) | "Calculating…"                                                        |
| RWKV-Curve answered but has no curve for the card       | "RWKV-Curve has no curve for this card yet. It gets one after your next answer." |
| the card has no answered review                         | "No curve yet. It appears after you answer this card."                      |
| FSRS-7 after a reset, with no answer since              | "This card was reset. The curve appears after you answer it again."         |
| FSRS-7 with no review on a later day                    | "No curve yet. This card needs a review on a later day."                    |

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
and one retrievability, labelled "Retrievability":

| Algorithm    | Stability       | Difficulty | Retrievability         | Forgetting curve |
| ------------ | --------------- | ---------- | ---------------------- | ---------------- |
| FSRS-7       | FSRS-7's S90    | FSRS-7's   | FSRS-7's               | FSRS-7's         |
| RWKV-Curve   | the curve's S90 | none       | the curve's recall now | RWKV-Curve's     |
| RWKV-Instant | none            | none       | RWKV's prediction      | none             |

RWKV-Curve's recall now is its stored curve (`ui.card-info-rwkv-curve`) at
the time since the card's latest answered review. While RWKV has no value
yet, the Retrievability row reads "Calculating…". Card info shows no other
RWKV rows: no second retrievability, no answer-button probabilities, no
next-S90 rows per button, no "R After Review" or "R After 10min", and no
retrievability source. In Simple mode (`ui.mode-switch`) card info shows no
stability, difficulty or retrievability at all; the forgetting curve stays.

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
prepared at all (`ui.fsrs7-no-rwkv-values`). The RWKV-Curve R here comes from RWKV's query of
each card now, while card info evaluates the curve stored at the card's
last review (`ui.card-info-one-algorithm`); the two differ, on Andrew's
collection by 0.04 in the median card and by up to 0.33.

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

## ui.stats-rwkv-scores-kept

Given the Stats page asks for the Retrievability graph again while nothing
RWKV reads has changed — the same collection and RWKV backend, the same
search, the same day, the same RWKV state generation, the same review inputs
and study queues — Clanki reuses the score map it published before instead of
scoring every card again. No clock ends the reuse: the map stands until one
of those changes. A new day, an answer, any change of the cards or the
queues, another search or deck, a new RWKV state generation, another backend
or collection and any other publication of a score map all end it, and the
next request scores again.

So the numbers the graph shows do not move with the seconds since each card's
last review: within one day they stay as they were when the map was built.
The scoring itself takes minutes on a large collection, so the map is already
minutes old when the page first draws it.

**Why:** Andrew, 2026-09-16: switching the Stats page between Simple and
Advanced mode, or its period between 12 months and all history, must not
start the RWKV calculation from zero again. RWKV's answer depends on neither
the mode nor the period. On Andrew's collection one pass costs 230 seconds
and 229 of them are RWKV scoring the 38,523 cards of the search. Andrew, the
same day, on dropping the earlier ten-minute limit: "p(recall) doesn't fall
*that* fast for most cards, so remove the time limit."

**Pinned by:** `test_prepare_stats_retrievability_scores_reuses_published_scores`,
`test_prepare_stats_retrievability_scores_keep_the_scores_however_long_the_wait`,
`test_prepare_stats_retrievability_scores_scores_again_for_another_search`,
`test_prepare_stats_retrievability_scores_scores_again_after_a_new_day`
(`qt/tests/test_rwkv_scheduler.py`)

## ui.stats-total-knowledge

Given the Stats page, in both Simple and Advanced mode, the Total Knowledge
graph shows, for each day from the first rating of a card in the page's
search through today, the sum of the cards' R that day under the
collection's algorithm only ("Known", blue; `sched.one-global-algorithm`,
`ui.stats-one-algorithm`), and it may show a second line, the cards of the
search rated at least once by that day ("Reviewed", grey, the upper bound).
It always covers the whole review history; the page's period does not apply.

Simple mode never draws "Reviewed" and has no control for it. Under the
graph it reads "This is Clanki's best estimate of how many cards you knew at
each point in your review history."

Advanced mode has a checkbox next to the legend, labelled "Reviewed", which
draws that line and is on when the graph loads, so Advanced mode looks as it
did before the checkbox. Under the graph it names the collection's algorithm
("Algorithm: FSRS-7", "Algorithm: RWKV-Curve" or "Algorithm: RWKV-Instant",
the names of the deck-options Algorithm list) and reads "Reviewed is an upper
bound on your knowledge: it counts every card you have ever rated, as if you
never forgot one."

The line under the title reads, in Advanced mode, "The number of cards you
would recall on each day (the sum of their retrievability), over your whole
review history."; in Simple mode it says the same without the word
retrievability: "The number of cards you would recall on each day (each card
counts as its probability of recall), over your whole review history."

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
finished result is kept for the session; closing the Stats window or
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

| Algorithm    | Its prediction of a rating                                        |
| ------------ | ----------------------------------------------------------------- |
| FSRS-7       | FSRS-7's forgetting curve at the memory state after the card's previous rating, at the days since it, with the card's preset parameters |
| RWKV-Curve   | the recall of the curve RWKV stored at the card's previous answered review, at the time since it |
| RWKV-Instant | RWKV-Instant's prediction of the card at that moment, from its state before the answer |

The predictions are not computed for the graph. Each algorithm writes them
per review while it runs, and the graph reads those rows: FSRS-7's when its
parameters are optimized, RWKV's when its state cache is built. A row counts
only when nothing that produced it was fitted on that very review:

| Algorithm | Rows that count                                                                |
| --------- | ------------------------------------------------------------------------------ |
| FSRS-7    | a validation fold first, else a run after the optimization; never the final fit |
| RWKV      | any role, because the weights are frozen and were trained on other collections  |

Each algorithm uses one role only, the first of its list that has any row
for the search, and the graph names the role it used. The two algorithms are
scored on the same ratings: a rating only one of them has a row for is left
out, and the graph says how many were left out for each reason. A rating
with no usable row for an algorithm is never filled in from a fresh
computation with today's parameters, because those parameters have seen the
rating.

Reading the rows does not start any computation. Ratings newer than the
newest stored prediction are left out, and the graph says "Predictions up to
&lt;date&gt;; N newer reviews are not scored yet", so it never silently drops
the newest reviews.

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

**Why:** Andrew, 2026-09-16: add the Search Stats Extended fork's
model-quality graphs, so that the algorithms can be compared on his own
reviews. The no-mixing rule is deliberately relaxed here and nowhere else,
because a comparison of one algorithm with itself says nothing; the honesty
rules (a name on every series, no fallback, an absent series with a reason)
are what keep the relaxation safe. All three curves at once on the AUC-ROC
graph, and the square drawing area with the labelled diagonal, are his
words. The rules about which stored rows count, and about scoring both
algorithms on the same ratings, are the RWKV session's: a prediction from a
model fitted on the very review it predicts flatters that model, and two
scores over two different sets of reviews cannot be compared. Reading the
stored rows rather than replaying is what the Search Stats Extended fork
does, and it is why a panel of hundreds of thousands of reviews opens at
once; a replay of the whole history costs minutes and now belongs to the
user's own rebuild, never to opening a page.

**Pinned by:** `only_rows_the_algorithm_had_not_seen_are_used`,
`both_algorithms_are_scored_on_the_same_ratings`,
`the_period_selects_the_ratings`,
`newer_ratings_than_the_stored_predictions_are_reported`
(`rslib/src/stats/review_metrics.rs`); `qt/tests/test_stats_metrics.py`;
`ts/routes/graphs/roc.test.ts`.

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
