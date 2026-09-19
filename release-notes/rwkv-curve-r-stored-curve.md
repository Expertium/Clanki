- Under RWKV-Curve, a card's retrievability in the Stats Retrievability
  graph, in `prop:rwkv-curve:r` searches (Browser, filtered decks,
  AnkiConnect) is now the forgetting curve RWKV stored at the card's last
  review, at the time since that review: the same curve RWKV-Curve schedules
  the card with, and the same value card info shows. Before, these places
  asked RWKV again for every card, which gave a different value (by 0.03 in
  the median card, up to 0.44) and took about three times as long. A card
  without a stored curve shows no value.
