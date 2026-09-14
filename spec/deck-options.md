# Deck options

## deck-options.scheduler-choice

Given the deck-options screen, the scheduler for a preset is chosen from one
dropdown with the values FSRS, RWKV-Curve and RWKV-Instant. SM-2 is not
selectable: the collection `fsrs` switch is on for every value. The value
maps onto the stored flags as follows, and nothing else:

| Value        | collection `fsrs` | preset `rwkv_review_enabled` | preset `rwkv_review_instant_order_enabled` |
| ------------ | ----------------- | ---------------------------- | ------------------------------------------ |
| FSRS         | on                | off                          | off                                        |
| RWKV-Curve   | on                | on                           | off                                        |
| RWKV-Instant | on                | off                          | on                                         |

Two stored states cannot be represented and are normalized when the preset is
shown, so that saving writes the represented state: a preset with both RWKV
flags on reads as RWKV-Curve and `rwkv_review_instant_order_enabled` is
cleared; a collection whose `fsrs` switch is off has it turned on, whatever
the preset's RWKV flags. Presets that are not opened are not touched, and
nothing is written until the user saves. The underlying flags, their storage in the
`jschoreels.rwkv` bag, and the scheduler behavior behind each one are
unchanged; only the way the screen sets them is.

**Why:** the three switches were independent and could be combined in ways
the user did not mean; desired retention was also only editable inside the
FSRS block even though RWKV reads it, which the dropdown resolves by keeping
FSRS on for every RWKV mode.

**Pinned by:** `ts/routes/deck-options/scheduler-choice.test.ts`.

## deck-options.advanced-view

Given the collection flag `deckOptionsAdvanced` (default off), the deck-options
screen hides the RWKV settings listed below and shows them only while the flag
is on. The flag is a collection-wide view preference, written immediately when
the switch at the top of the page changes, and is not part of the deck-options
save. Hidden settings keep their stored values and keep taking effect.

Hidden: keep RWKV intervals in answer order; minimum reviews per day; faster
approximate queue updates; queue update interval; update queue after reviewing;
minimum other reviews and minimum seconds before a same-day repeat; predict R
for new cards from creation time; dynamic preset add-on support; the Rebuild
RWKV State, Recompute Calibration and Compare with FSRS actions.

Always visible while an RWKV mode is selected: the same-day repeat switch
(RWKV-Instant) and the Reschedule cards action (RWKV-Curve).

**Why:** plan item 2 — a Simplified view is the default; the remaining RWKV
knobs have defaults that suit nearly everyone.

**Pinned by:** `deck_options_advanced_flag_is_reported`
(`rslib/src/deckconfig/update.rs`) for the flag plumbing. The visibility
itself is markup and has no unit test.
