- AnkiConnect: `answerCards` and `gradeNow` now answer a card of any
  algorithm. They go through the same Grade Now as the Browser's Cards >
  Grade Now, so a card RWKV-Curve schedules gets RWKV-Curve's interval and
  stability, the ones the reviewer gives it, and never FSRS-7's. Before,
  `answerCards` gave `false` and `gradeNow` an error for every RWKV-Curve
  card. A card RWKV-Curve has no intervals for yet (for example while the
  RWKV state is still loading) is still not answered: `answerCards` gives
  `false` for it and `gradeNow` fails naming it, after it graded the other
  cards. `guiAnswerCard` now gives `false` in that case too, instead of
  `true` for an answer the reviewer ignored.
