- RWKV-Instant: an answer with an FSRS-7 interval under a day (such as Again
  on a new card) no longer puts the card in the learning queue, where FSRS-7
  decided when it came back. The card waits for RWKV-Instant's score instead.
- No algorithm computes a probability of recall for a card's first review any
  more: RWKV-Instant's value for it knew only the deck, the preset and the
  creation date. The new-card gather orders "Ascending/Descending
  retrievability" are gone (a preset that used one gathers by deck), and new
  cards get no RWKV-Instant value in the Stats retrievability graph, `is:new`
  searches, filtered decks, card info or AnkiConnect.
- The Stats model-quality graphs (AUC-ROC, calibration, UM+) leave out a
  card's first rating after a Forget and after a new learning start, as they
  already did its first rating ever: no algorithm knows the card at that
  point, and RWKV-Instant was the only one scored on those ratings.
