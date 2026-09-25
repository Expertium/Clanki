- RWKV replays the reviews of deleted cards, as the model was trained: they
  shape the review counts and the collection-wide state that every other
  card's prediction reads. Before, they were left out, so every prediction
  came from a history the model never saw in training.
- RWKV's code for each card, note, deck and preset now depends only on its
  id. Opening a screen that asks RWKV about a card it had not seen (a new
  card's Card Info, for example) no longer changes the predictions of other
  cards, and live predictions match the ones Stats records.
- The RWKV states and recordings are rebuilt once after this update, in the
  background.
