# Scheduling

## sched.sibling-dispersal-gate

Given a card whose home preset has **"Bury review siblings"** enabled and the
load balancer enabled, the day chosen for its next review interval is picked
from the fuzz range with a penalty on days already holding a sibling: ×0.000001
on the sibling's day, ×0.2 / ×0.4 / ×0.6 / ×0.8 at ±1 / ±2 / ±3 / ±4 days.
"Bury new siblings" and "Bury interday learning siblings" have no effect on
fuzz; they only bury cards in the queue.

**Why:** inherited from upstream Anki; users read the three toggles as one
feature, so the asymmetry is recorded here rather than rediscovered.

**Pinned by:** `external_intervals_are_dispersed_away_from_siblings`
(`rslib/src/scheduler/states/load_balancer.rs`) for the penalty and the gate;
the gate's wiring is at `rslib/src/scheduler/answering/mod.rs`
(`review_load_balancer_ctx`).

## sched.rwkv-curve-fuzz

Given a card whose preset has **"Use RWKV-Curve for answer intervals"**
enabled, the interval RWKV-Curve supplies for each button is treated as the
unfuzzed target and passed through the same review fuzz as an FSRS interval
for the same card: the same fuzz range, the same load balancer, the same
sibling dispersal (`sched.sibling-dispersal-gate`), the same 90-day
load-balance limit, and the same floors — Again is clamped but not fuzzed;
Hard, Good and Easy each keep the previous interval when it still lies within
the configured fuzz range; and Good/Easy sit at least one day above the fuzzed
button before them. The
resulting fuzz delta is recorded on the state and shown above the answer
buttons when that preference is on.

Before this entry, RWKV-Curve wrote its interval over the already-fuzzed FSRS
state and set the delta to 0, so RWKV-Curve users got no fuzz and no sibling
dispersal at all.

**Why:** fuzz and sibling dispersal are properties of the _scheduling
outcome_, not of FSRS; switching the interval source must not switch them off.

**Pinned by:** `interval_overrides::test::*` and
`fuzz_review_intervals_uses_review_floors_and_clamps` (Rust);
`test_fuzz_review_interval_overrides_uses_backend_review_fuzz`,
`test_fuzz_review_interval_overrides_only_sends_supplied_ratings`,
`test_apply_review_interval_overrides_records_fuzz_deltas`
(`qt/tests/test_rwkv_scheduler.py`).
