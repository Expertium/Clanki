- The Browser's Retrievability, Stability and Difficulty columns show only
  the collection's algorithm's values. Under RWKV-Curve, Retrievability is the
  card's stored curve now and Stability its S90; under RWKV-Instant,
  Retrievability is RWKV-Instant's value for the card now. Difficulty is
  FSRS-7's only. Before, both RWKV algorithms showed FSRS-7's values. The
  RWKV values are computed in the background for the rows on screen, so
  scrolling does not wait for them, and sorting by Retrievability sorts by
  the algorithm's own value.
- Retrievability ("Probability of recall") is one of the Browser's Simple
  mode columns now.
- RWKV values that Clanki keeps (the Stats page's scores, which Browser
  searches share) are computed again once they could be out of date: after
  any review, undo or reset, on a new day, and after a time that depends on
  how recently the card was reviewed (from never kept, for a card reviewed
  under ten minutes ago, to an hour). A filtered deck scores its cards when
  it is built.
