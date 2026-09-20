- Under RWKV-Curve and RWKV-Instant, a filtered deck ordered by "Relative
  overdueness" now ranks cards by RWKV's own retrievability divided by the
  desired retention, as RWKV's review order does. Before, it used FSRS-7's
  memory state, which mixed the two algorithms.
