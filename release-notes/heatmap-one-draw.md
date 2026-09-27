- Going back to the deck list or a deck's overview after an answer, an undo or
  a bury now draws the page once, with the review heatmap, instead of twice
  (first without it). Clanki computes the heatmap together with the deck
  counts and waits up to 100 ms for it. On a 1.3-million-review collection the
  finished page came 10 ms sooner on the deck list and on the overview.
- The heatmap's squares now animate only the first time the heatmap appears
  after a collection is opened. Later draws show them at once. Andrew left the
  choice to Claude, 2026-09-27; reason: repeated animation on every return
  looks like a slow load.
