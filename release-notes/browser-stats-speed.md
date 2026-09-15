- The Stats page, sorting the Browser by Retrievability or Stability, and
  `prop:r`/`prop:s` searches are 2 to 3.5 times faster on a large
  collection: FSRS-7's retrievability is computed directly instead of
  through the machine-learning library, and the cards behind these
  searches load in one step (a 159k-card collection: the Stats page 1.5 to
  0.7 s, sorting by Retrievability 1.8 to 0.6 s, `prop:r<0.9` 1.7 to 0.5 s).
  The values shown do not change.
- The Browser redraws its table faster (a full redraw 46 to 39 ms), and
  finds the rows to select again after a search about twice as fast.
