- Saving deck options that give a deck another preset no longer shows a
  "Processing..." window. The save only marks the FSRS-7 predictions of the
  Stats graphs as out of date, and the background pass deletes and writes
  them again in short steps. Until then the graphs say that FSRS-7's
  predictions are being computed.
- The preset that a deck leaves now gets new FSRS-7 predictions too. Before,
  it kept predictions from folds that still included the moved deck. An undo
  or redo of such a save also marks both presets, so the graphs never show
  predictions of the other state.
