- With FSRS-7 and a Retrievability or Relative overdueness review order,
  the next card appears sooner after each answer: the study queue, which
  is rebuilt after every answer, computes each card's retrievability
  directly instead of through the machine-learning library (a 159k-card
  collection with about 20k due cards: 543 to 104 ms per answer with the
  Retrievability order, 156 to 110 ms with Relative overdueness).
  Building or rebuilding a filtered deck with these orders is 2.5 to 3
  times faster (a 1000-card deck from 20k due cards: 561 to 180 ms). The
  order of the cards does not change.
