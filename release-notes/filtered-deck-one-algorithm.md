- A filtered deck ordered by retrievability now uses only the collection's
  algorithm. Under RWKV-Curve it orders by each card's stored RWKV-Curve
  curve, under RWKV-Instant by RWKV-Instant's value; the deck scores its own
  cards before it is built. Before, it could take RWKV-Instant's value under
  RWKV-Curve, or FSRS-7's or SM-2's value for a card that the last Stats or
  Browser search had not scored. A card without a value from the algorithm
  now goes to the end of the deck.
