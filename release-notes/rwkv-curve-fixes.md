- RWKV-Curve and RWKV-Instant: a preset that still stores "Easy cards first"
  or "Difficult cards first" now studies its reviews by descending
  retrievability, the order Deck Options shows for it, instead of by FSRS-7
  difficulty.
- RWKV-Curve: card info and AnkiConnect now measure a card's retrievability
  from its last review that RWKV learned from. A later preview in a filtered
  deck no longer makes the value too high.
- RWKV-Curve and RWKV-Instant: the first answer after Forget now starts the
  card fresh in RWKV's live state, as a rebuild does, instead of continuing
  from the card's earlier reviews.
- RWKV-Curve: the retrievability and relative-overdueness review orders now
  compute each card's retrievability from its stored curve when the queue
  is built. The order no longer changes after a Browser search or a visit to
  the Stats page, and no longer uses a value scored hours earlier.
