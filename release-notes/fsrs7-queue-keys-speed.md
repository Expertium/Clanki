- With FSRS-7 and a Retrievability or Relative overdueness review order,
  building the study queue is faster: each card's retrievability is
  computed directly instead of through the machine-learning library (a
  159k-card collection with about 20k due cards: 543 to 104 ms per build
  with the Retrievability order, 156 to 110 ms with Relative overdueness).
  The queue is built when it runs out or is reset, for example on a new
  day. Building or rebuilding a filtered deck with these orders is 2.5 to
  3 times faster (a 1000-card deck from 20k due cards: 561 to 180 ms). The
  order of the cards does not change.
