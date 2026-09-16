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
deck menu (the gear next to a deck) has no RWKV submenu (Reschedule With
RWKV-Curve, Reschedule All Decks) and no Advance or Postpone entries
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
log time from one minute to 100 years, joined by straight lines.

Card info for such a card shows no other FSRS-7 value either: no
"Difficulty" row, and its one "Retrievability" row is the curve's recall
now (`ui.card-info-one-algorithm`). The page data of the reviews before the latest answered one carries no
memory state, so no FSRS-7 stability of those reviews reaches the page.
Cards of FSRS-7 presets draw FSRS-7's curve with its S90 for every review;
RWKV-Instant cards draw none (`ui.card-info-one-algorithm`).

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

| Algorithm    | Stability               | Difficulty | Retrievability             | Forgetting curve |
| ------------ | ----------------------- | ---------- | -------------------------- | ---------------- |
| FSRS-7       | FSRS-7's S90            | FSRS-7's   | FSRS-7's                   | FSRS-7's         |
| RWKV-Curve   | the curve's S90         | none       | the curve's recall now     | RWKV-Curve's     |
| RWKV-Instant | none                    | none       | RWKV's prediction          | none             |

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

| Algorithm    | Retrievability graph      | Difficulty graph | Stability graph |
| ------------ | ------------------------- | ---------------- | --------------- |
| FSRS-7       | FSRS-7's R                | shown            | shown           |
| RWKV-Curve   | the RWKV-Curve head's R   | none             | shown (S90)     |
| RWKV-Instant | RWKV-Instant's R          | none             | none            |

Under RWKV there is no FSRS-7 series beside RWKV's and no FSRS-7 value for
a card RWKV has not scored. While RWKV has not scored the page's search yet
(its state loading or warming up), the Retrievability graph shows
"Calculating…" instead of values, and the page asks again every 2 seconds
until the scores arrive; under FSRS-7 no RWKV score is prepared at all
(`ui.fsrs7-no-rwkv-values`). The RWKV-Curve R here comes from RWKV's query of
each card now, while card info evaluates the curve stored at the card's
last review (`ui.card-info-one-algorithm`); the two differ, on Andrew's
collection by 0.04 in the median card and by up to 0.33.

**Why:** Andrew, 2026-09-15: never mix two algorithms in one display; while
RWKV is not ready, show "…" or "Calculating…" rather than FSRS-7's values;
RWKV has no difficulty and RWKV-Instant no stability.

**Pinned by:** `retrievability_graph_uses_rwkv_scores_for_matching_search`,
`fsrs7_stats_show_no_rwkv_values_and_rwkv_curve_uses_the_curve`
(`rslib/src/stats/graphs/retrievability.rs`);
`test_rwkv_curve_collection_active_reads_the_algorithm`
(`qt/tests/test_rwkv_scheduler.py`); "while RWKV calculates, the graph shows
and says so, with no other values" (`ts/routes/graphs/retrievability.test.ts`).

## ui.stats-rwkv-scores-kept

Given the Stats page asks for the Retrievability graph a second time while
nothing RWKV reads has changed — the same search, the same day, the same
RWKV state, the same review inputs and study queues — Clanki reuses the
score map it published the first time instead of scoring every card again,
for up to 10 minutes after that first map was published. Any answer, any
change of the cards or the queues, a new day, another search and any other
publication of a score map end the reuse, and the next request scores again.

The graph then shows RWKV's R as of the moment the map was built, not of the
moment of the request. The scoring itself takes minutes on a large
collection, so the first map is already that old when the page first draws
it.

**Why:** Andrew, 2026-09-16: switching the Stats page between Simple and
Advanced mode, or its period between 12 months and all history, must not
start the RWKV calculation from zero again. RWKV's answer depends on neither
the mode nor the period. On Andrew's collection one pass costs 230 seconds
and 229 of them are RWKV scoring the 38,523 cards of the search.

**Pinned by:** `test_prepare_stats_retrievability_scores_reuses_published_scores`,
`test_prepare_stats_retrievability_scores_scores_again_for_another_search`,
`test_prepare_stats_retrievability_scores_scores_again_after_the_reuse_window`,
`test_prepare_stats_retrievability_scores_scores_again_after_a_new_day`
(`qt/tests/test_rwkv_scheduler.py`)

## ui.stats-total-knowledge

Given the Stats page, in both Simple and Advanced mode, the Total Knowledge
graph shows, for each day from the first rating of a card in the page's
search through today, two lines: the cards of the search rated at least
once by that day (the upper bound), and the sum of the cards' R that day
under the collection's algorithm only (`sched.one-global-algorithm`,
`ui.stats-one-algorithm`). It always covers the whole review history; the
page's period does not apply.

A day is a scheduler day ("next day starts at" applies). A card's R on a
day comes from its last event on or before that day: a rating that day
gives 1; an earlier rating gives the algorithm's R at the whole days since
it; a reset (Forget) gives 0 until the card's next rating (the card stays in
the bound). Ratings are answers that affect scheduling: manual reschedules,
resets and cram answers are not ratings.

| Algorithm    | R after a rating                                                      | Drawn                              |
| ------------ | --------------------------------------------------------------------- | ---------------------------------- |
| FSRS-7       | FSRS-7's forgetting curve at the memory state after that rating       | at once                            |
| RWKV-Curve   | the curve RWKV stores at that rating, at the whole days since it      | day by day, oldest first           |
| RWKV-Instant | RWKV-Instant's prediction for the card on that day                    | day by day, oldest first           |

FSRS-7 replays each card's whole history with its preset's FSRS-7
parameters: every part of the history starts as the card's own memory state
does after a reset or a relearn, so the days before a reset follow that part's
replay. A rating FSRS-7 has no state for (a history without a learning step
before the first interday review) gives 0 on the days after it. The
replay runs in the backend off the main thread; while it runs the graph
reads "Calculating…".

Under RWKV the upper bound shows at once. A background RWKV job replays the
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
Simple mode too.

**Pinned by:** `fsrs7_sums_match_the_cards_historical_memory_states`,
`total_knowledge_covers_the_whole_history`,
`a_reset_zeroes_r_until_the_next_rating`,
`rwkv_collections_get_the_upper_bound_only`
(`rslib/src/stats/total_knowledge.rs`); FSRS-7's curve is the scalar copy
pinned by `scalar_curve_is_bit_identical_to_the_tensor_path`
(`rslib/src/scheduler/fsrs/curve.rs`);
`curve_day_sums_from_warm_up_are_the_stored_curves` (`rslib/src/rwkv/mod.rs`);
`qt/tests/test_total_knowledge.py`;
`ts/routes/graphs/total-knowledge.test.ts`.

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
